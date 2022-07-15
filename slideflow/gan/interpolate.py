"""Tool to assist with embedding interpolation for a class-conditional GAN."""

from typing import (Generator, List, Optional, Tuple, Union,
                    TYPE_CHECKING, Any)

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import slideflow as sf
import torch
from PIL import Image
from slideflow.gan.stylegan2 import embedding, utils
from slideflow.gan.utils import crop
from tqdm import tqdm
from functools import partial

if TYPE_CHECKING:
    import tensorflow as tf

class StyleGAN2Interpolator:

    def __init__(
        self,
        gan_pkl: str,
        start: int,
        end: int,
        device: torch.device,
        gan_um: int,
        gan_px: int,
        target_um: int,
        target_px: int,
        **gan_kwargs
    ) -> None:
        """Coordinates class and embedding interpolation for a trained
        class-conditional StyleGAN2.

        Args:
            gan_pkl (str): Path to saved network pkl.
            start (int): Starting class index.
            end (int): Ending class index.
            device (torch.device): Torch device.
        """
        self.E_G, self.G = embedding.load_embedding_gan(gan_pkl, device)
        self.device = device
        self.gan_kwargs = gan_kwargs
        self.embed0, self.embed1 = embedding.get_class_embeddings(
            self.G,
            start=start,
            end=end,
            device=device
        )
        self.features = None  # type: Optional[sf.model.Features]
        self.normalizer = None
        self.target_px = target_px
        self.crop_kw = dict(
            gan_um=gan_um,
            gan_px=gan_px,
            target_um=target_um,
        )

    def _crop_and_convert_to_uint8(self, img: torch.Tensor) -> Any:
        """Convert a batch of GAN images to a resized/cropped uint8 tensor.

        Args:
            img (torch.Tensor): Raw GAN output images (torch.float32)

        Returns:
            Any: GAN images (torch.uint8)
        """
        if sf.backend() == 'tensorflow':
            import tensorflow as tf
            dtype = tf.uint8
        elif sf.backend() == 'torch':
            import torch
            dtype = torch.uint8
        else:
            raise ValueError(f"Unrecognized backend {sf.backend()}")
        img = crop(img, **self.crop_kw)  # type: ignore
        return sf.io.convert_dtype(img, dtype)

    def _preprocess_from_uint8(
        self,
        img: Any,
        normalize: bool,
        standardize: bool,
    ) -> Any:
        """Convert and resize a batch of uint8 tensors to
        standardized/normalized tensors ready for input to the
        classifier/feature model.

        Args:
            img (Any): GAN images (uint8)
            normalize (bool): Normalize the images.
            standardize (bool): Standardize the images.

        Returns:
            Any: Resized GAN images (uint8 or float32 if standardize=True)
        """
        normalizer = self.normalizer if normalize else None
        if sf.backend() == 'tensorflow':
            return sf.io.tensorflow.preprocess_uint8(
                img,
                normalizer=normalizer,
                standardize=standardize,
                resize_px=self.target_px)['tile_image']
        elif sf.backend() == 'torch':
            return sf.io.torch.preprocess_uint8(
                img,
                normalizer=normalizer,
                standardize=standardize,
                resize_px=self.target_px)
        else:
            raise ValueError(f"Unrecognized backend {sf.backend()}")

    def _standardize(self, img: Any) -> Any:
        """Standardize image from uint8 to float.

        Args:
            img (Any): uint8 image tensor.

        Returns:
            Any: Standardized float image tensor.
        """
        if sf.backend() == 'tensorflow':
            import tensorflow as tf
            return sf.io.convert_dtype(img, tf.float32)
        elif sf.backend() == 'torch':
            import torch
            return sf.io.convert_dtype(img, torch.float32)
        else:
            raise ValueError(f"Unrecognized backend {sf.backend()}")

    def _build_gan_dataset(self, generator):
        """_summary_

        Args:
            generator (Generator): Python generator which yields cropped
                (but not resized) uint8 tensors.

        Returns:
            Iterable: _description_
        """
        if sf.backend() == 'tensorflow':
            import tensorflow as tf

            sig = tf.TensorSpec(shape=(None, None, None, 3), dtype=tf.uint8)
            dts = tf.data.Dataset.from_generator(generator, output_signature=sig)
            return dts.map(
                partial(
                    sf.io.tensorflow.preprocess_uint8,
                    normalizer=self.normalizer,
                    resize_px=self.target_px),
                num_parallel_calls=tf.data.AUTOTUNE,
                deterministic=True
            )
        elif sf.backend() == 'torch':
            return map(
                partial(
                    sf.io.torch.preprocess_uint8,
                    normalizer=self.normalizer,
                    resize_px=self.target_px),
                generator())
        else:
            raise ValueError(f"Unrecognized backend {sf.backend()}")

    def z(self, seed: int) -> torch.Tensor:
        """Returns a noise tensor for a given seed.

        Args:
            seed (int): Seed.

        Returns:
            torch.tensor: Noise tensor for the corresponding seed.
        """
        return utils.noise_tensor(seed, self.E_G.z_dim).to(self.device)  # type: ignore

    def set_feature_model(
        self,
        path: str,
        layers: Union[str, List[str]] = 'postconv'
    ) -> None:
        """Configures a classifier model to be used for generating features
        and predictions during interpolation.

        Args:
            path (str): Path to trained model.
            layers (Union[str, List[str]], optional): Layers from which to
                calculate activations for interpolated images.
                Defaults to 'postconv'.
        """
        self.features = sf.model.Features(path, layers=layers, include_logits=True)
        self.normalizer = self.features.wsi_normalizer  # type: ignore

    def seed_search(
        self,
        seeds: List[int],
        batch_size: int = 32,
        verbose: bool = False
    ) -> pd.core.frame.DataFrame:
        """Generates images for starting and ending classes for many seeds,
        calculating layer activations from a set classifier.

        Args:
            seeds (List[int]): Seeds.
            batch_size (int, optional): Batch size for GAN during generation.
                Defaults to 32.
            verbose (bool, optional): Verbose output. Defaults to False.

        Raises:
            Exception: If classifier model has not been been set with
                .set_feature_model()

        Returns:
            pd.core.frame.DataFrame: Dataframe of results.
        """

        if self.features is None:
            raise Exception("Feature model not set; use .set_feature_model()")

        predictions = {0: [], 1: []}  # type: ignore
        features = {0: [], 1: []}  # type: ignore
        swap_labels = []
        img_seeds = []

        # --- GAN-Classifier pipeline ---------------------------------------------
        def gan_generator(embedding):
            def generator():
                for seed_batch in sf.util.batch(seeds, batch_size):
                    z = torch.stack([
                        utils.noise_tensor(s, z_dim=self.E_G.z_dim)[0]
                        for s in seed_batch]
                    ).to(self.device)
                    img_batch = self.E_G(z, embedding.expand(z.shape[0], -1), **self.gan_kwargs)
                    yield self._crop_and_convert_to_uint8(img_batch)
            return generator

        # --- Input data stream ---------------------------------------------------
        gan_embed0_dts = self._build_gan_dataset(gan_generator(self.embed0))
        gan_embed1_dts = self._build_gan_dataset(gan_generator(self.embed1))

        # Calculate classifier features for GAN images created from seeds.
        # Calculation happens in batches to improve computational efficiency.
        # noise + embedding -> GAN -> Classifier -> Predictions, Features
        pb = tqdm(total=len(seeds), leave=False)
        seeds_and_embeddings = zip(
            sf.util.batch(seeds, batch_size),
            gan_embed0_dts,
            gan_embed1_dts
        )
        for (seed_batch, embed0_batch, embed1_batch) in seeds_and_embeddings:

            features0, pred0 = self.features(embed0_batch)
            features1, pred1 = self.features(embed1_batch)
            pred0 = pred0[:, 0].numpy()
            pred1 = pred1[:, 0].numpy()
            features0 = np.reshape(features0.numpy(), (len(seed_batch), -1)).astype(np.float32)
            features1 = np.reshape(features1.numpy(), (len(seed_batch), -1)).astype(np.float32)

            # For each seed in the batch, determine if there ids "class-swapping",
            # where the GAN class label matches the classifier prediction.
            #
            # This may not happen 100% percent of the time even with a perfect GAN
            # and perfect classifier, since both classes have images that are
            # not class-specific (such as empty background, background tissue, etc)
            for i in range(len(seed_batch)):
                img_seeds += [seed_batch[i]]
                predictions[0] += [pred0[i]]
                predictions[1] += [pred1[i]]
                features[0] += [features0[i]]
                features[1] += [features1[i]]

                # NOTE: This logic assumes predictions are discretized at 0,
                # which will not be true for categorical outcomes.
                if (pred0[i] < 0) and (pred1[i] > 0):
                    # Class-swapping is observed for this seed.
                    if (pred0[i] < -0.5) and (pred1[i] > 0.5):
                        # Strong class swapping.
                        tail = " **"
                        swap_labels += ['strong_swap']
                    else:
                        # Weak class swapping.
                        tail = " *"
                        swap_labels += ['weak_swap']
                elif (pred0[i] > 0) and (pred1[i] < 0):
                    # Predictions are oppositve of what is expected.
                    tail = " (!)"
                    swap_labels += ['no_swap']
                else:
                    tail = ""
                    swap_labels += ['no_swap']
                if verbose:
                    tqdm.write(f"Seed {seed_batch[i]:<6}: {pred0[i]:.2f}\t{pred1[i]:.2f}{tail}")
            pb.update(len(seed_batch))
        pb.close()

        # Convert to dataframe.
        df = pd.DataFrame({
            'seed': pd.Series(img_seeds),
            'pred_start': pd.Series(predictions[0]),
            'pred_end': pd.Series(predictions[1]),
            'features_start': pd.Series(features[0]).astype(object),
            'features_end': pd.Series(features[1]).astype(object),
            'class_swap': pd.Series(swap_labels),
        })
        return df

    def plot_comparison(
        self,
        seeds: Union[int, List[int]],
        titles: Optional[List[str]] = None
    ) -> None:
        """Plots side-by-side comparison of images from the starting
        and ending interpolation classes.

        Args:
            seeds (int or list(int)): Seeds to display.
        """
        if not isinstance(seeds, list):
            seeds = [seeds]
        if titles is None:
            titles = ['Start', 'End']
        assert len(titles) == 2

        scale = 5
        fig, ax = plt.subplots(len(seeds), 2, figsize=(2 * scale, len(seeds) * scale))
        for s, seed in enumerate(seeds):
            # First image (starting embedding)
            img0 = self.generate_start(seed)
            img0 = self._crop_and_convert_to_uint8(img0)
            img0 = self._preprocess_from_uint8(img0, standardize=False, normalize=False)
            img0 = Image.fromarray(img0.numpy()[0])

            # Second image (ending embedding)
            img1 = self.generate_end(seed)
            img1 = self._crop_and_convert_to_uint8(img1)
            img1 = self._preprocess_from_uint8(img1, standardize=False, normalize=False)
            img1 = Image.fromarray(img1.numpy()[0])

            if len(seeds) == 1:
                _ax0 = ax[0]
                _ax1 = ax[1]
            else:
                _ax0 = ax[s, 0]
                _ax1 = ax[s, 1]
            if s == 0:
                _ax0.set_title(titles[0])
                _ax1.set_title(titles[1])
            _ax0.imshow(img0)
            _ax1.imshow(img1)
            _ax0.axis('off')
            _ax1.axis('off')

        fig.subplots_adjust(wspace=0.05, hspace=0)

    def generate(self, seed: int, embedding: torch.Tensor) -> torch.Tensor:
        """Generate an image from a given embedding.

        Args:
            seed (int): Seed for noise vector.
            embedding (torch.Tensor): Class embedding.

        Returns:
            torch.Tensor: Image (float32, shape=(1, 3, height, width))
        """
        return self.E_G(self.z(seed), embedding, **self.gan_kwargs)

    def generate_start(self, seed: int) -> torch.Tensor:
        """Generate an image from the starting class.

        Args:
            seed (int): Seed for noise vector.

        Returns:
            torch.Tensor: Image (float32, shape=(1, 3, height, width))
        """
        return self.generate(seed, self.embed0)

    def generate_end(self, seed: int) -> torch.Tensor:
        """Generate an image from the ending class.

        Args:
            seed (int): Seed for noise vector.

        Returns:
            torch.Tensor: Image (float32, shape=(1, 3, height, width))
        """
        return self.generate(seed, self.embed1)

    def generate_np_from_embedding(
        self,
        seed: int,
        embedding: torch.Tensor
    ) -> np.ndarray:
        """Generate a numpy image from a given embedding.

        Args:
            seed (int): Seed for noise vector.
            embedding (torch.Tensor): Class embedding.

        Returns:
            np.ndarray: Image (uint8, shape=(height, width, 3))
        """
        img = self.generate(seed, embedding)
        img = img.permute(0, 2, 3, 1) * 127.5 + 128
        img = img.clamp(0, 255).to(torch.uint8).cpu().numpy()[0]
        return img  # type: ignore

    def generate_np_start(self, seed: int) -> np.ndarray:
        """Generate a numpy image from the starting class.

        Args:
            seed (int): Seed for noise vector.

        Returns:
            np.ndarray: Image (uint8, shape=(height, width, 3))
        """
        return self.generate_np_from_embedding(seed, self.embed0)

    def generate_np_end(self, seed: int) -> np.ndarray:
        """Generate a numpy image from the ending class.

        Args:
            seed (int): Seed for noise vector.

        Returns:
            np.ndarray: Image (uint8, shape=(height, width, 3))
        """
        return self.generate_np_from_embedding(seed, self.embed1)

    def generate_tf_from_embedding(
        self,
        seed: int,
        embedding: torch.Tensor
    ) -> Tuple["tf.Tensor", "tf.Tensor"]:
        """Create a processed Tensorflow image from the GAN output from a given
        seed and embedding.

        Args:
            seed (int): Seed for noise vector.
            embedding (torch.tensor): Class embedding.

        Returns:
            tf.Tensor: Unprocessed resized image, uint8.

            tf.Tensor: Processed resized image, standardized and normalized.
        """
        import tensorflow as tf

        gan_out = self.generate(seed, embedding)
        gan_out = self._crop_and_convert_to_uint8(gan_out)
        gan_out = self._preprocess_from_uint8(gan_out, standardize=False, normalize=True)
        standardized = self._standardize(gan_out)
        return gan_out[0], standardized[0]

    def generate_tf_start(self, seed: int) -> Tuple["tf.Tensor", "tf.Tensor"]:
        """Create a processed Tensorflow image from the GAN output of a given
        seed and the starting class embedding.

        Args:
            seed (int): Seed for noise vector.

        Returns:
            tf.Tensor: Unprocessed image (tf.Tensor), uint8.

            tf.Tensor: Processed image (tf.Tensor), standardized and normalized.
        """
        return self.generate_tf_from_embedding(seed, self.embed0)

    def generate_tf_end(self, seed: int) -> Tuple["tf.Tensor", "tf.Tensor"]:
        """Create a processed Tensorflow image from the GAN output of a given
        seed and the ending class embedding.

        Args:
            seed (int): Seed for noise vector.

        Returns:
            tf.Tensor: Unprocessed resized image, uint8.

            tf.Tensor: Processed resized image, standardized and normalized.
        """
        return self.generate_tf_from_embedding(seed, self.embed1)

    def class_interpolate(self, seed: int, steps: int = 100) -> Generator:
        """Sets up a generator that returns images during class embedding
        interpolation.

        Args:
            seed (int): Seed for random noise vector.
            steps (int, optional): Number of steps for interpolation.
                Defaults to 100.

        Returns:
            Generator: Generator which yields images (torch.tensor, uint8)
                during interpolation.

        Yields:
            Generator: images (torch.tensor, dtype=uint8)
        """
        return embedding.class_interpolate(
            self.E_G,
            self.z(seed),
            self.embed0,
            self.embed1,
            device=self.device,
            steps=steps,
            **self.gan_kwargs
        )

    def linear_interpolate(self, seed: int, steps: int = 100) -> Generator:
        """Sets up a generator that returns images during linear label
        interpolation.

        Args:
            seed (int): Seed for random noise vector.
            steps (int, optional): Number of steps for interpolation.
                Defaults to 100.

        Returns:
            Generator: Generator which yields images (torch.tensor, uint8)
                during interpolation.

        Yields:
            Generator: images (torch.tensor, dtype=uint8)
        """
        return embedding.linear_interpolate(
            self.G,
            self.z(seed),
            device=self.device,
            steps=steps,
            **self.gan_kwargs
        )

    def interpolate_and_predict(
        self,
        seed: int,
        steps: int = 100,
    ) -> Tuple[List, ...]:
        """Interpolates between starting and ending classes for a seed,
        recording raw images, processed images, and predictions.

        Args:
            seed (int): Seed for random noise vector.
            steps (int, optional): Number of steps during interpolation.
                Defaults to 100.

        Returns:
            Tuple[List, ...]: Raw images, processed images, and predictions.
        """
        imgs = []
        proc_imgs = []
        preds = []

        for img in tqdm(self.class_interpolate(seed, steps), total=steps):
            img = torch.from_numpy(np.expand_dims(img, axis=0)).permute(0, 3, 1, 2)
            img = (img / 127.5) - 1
            img = self._crop_and_convert_to_uint8(img)
            img = self._preprocess_from_uint8(img, standardize=False, normalize=True)
            processed_img = self._standardize(img)
            img = img.numpy()[0]
            if self.features is not None:
                pred = self.features(processed_img)[-1].numpy()
                preds += [pred[0][0]]
            imgs += [img]
            proc_imgs += [processed_img[0]]

        sns.lineplot(x=range(len(preds)), y=preds, label="Prediction")
        plt.axhline(y=0, color='black', linestyle='--')
        plt.title("Prediction during interpolation")
        plt.xlabel("Interpolation Step (Start -> End)")
        plt.ylabel("Prediction")

        return imgs, proc_imgs, preds
