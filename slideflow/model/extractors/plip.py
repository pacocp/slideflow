import torch
import slideflow as sf
import numpy as np
from torchvision import transforms

try:
    from transformers import CLIPModel
    from transformers.image_utils import OPENAI_CLIP_MEAN, OPENAI_CLIP_STD
except ImportError:
    raise ImportError(
        "The PLIP feature extractor requires the 'transformers' package. "
        "You can install it with 'pip install transformers'."
    )

from ..base import BaseFeatureExtractor
from ._slide import features_from_slide

# -----------------------------------------------------------------------------

class CLIPImageFeatures(torch.nn.Module):

    def __init__(self, weights='vinid/plip'):
        super().__init__()
        self._model = CLIPModel.from_pretrained(weights)

    def forward(self, x):
        x = self._model.get_image_features(x)
        return x


class PLIPFeatures(BaseFeatureExtractor):
    """
    PLIP pretrained feature extractor.
    Feature dimensions: 512
    GitHub: https://github.com/PathologyFoundation/plip
    """

    tag = 'plip'
    license_statement = "No license provided by the authors."
    citation = """
@article{huang2023visual,
    title={A visual--language foundation model for pathology image analysis using medical Twitter},
    author={Huang, Zhi and Bianchi, Federico and Yuksekgonul, Mert and Montine, Thomas J and Zou, James},
    journal={Nature Medicine},
    pages={1--10},
    year={2023},
    publisher={Nature Publishing Group US New York}
}
"""

    def __init__(self, device=None, center_crop=False):
        super().__init__(backend='torch')

        from slideflow.model import torch_utils
        self.device = torch_utils.get_device(device)
        self.model = CLIPImageFeatures("vinid/plip")
        self.model.eval()
        self.model.to(self.device)

        # ---------------------------------------------------------------------
        self.num_features = 512
        self.num_classes = 0
        all_transforms = [transforms.CenterCrop(224)] if center_crop else []
        all_transforms += [
            transforms.Lambda(lambda x: x / 255.),
            transforms.Normalize(
                mean=OPENAI_CLIP_MEAN,
                std=OPENAI_CLIP_STD),
        ]
        self.transform = transforms.Compose(all_transforms)
        self.preprocess_kwargs = dict(standardize=False)
        self._center_crop = center_crop
        # ---------------------------------------------------------------------

    def __call__(self, obj, **kwargs):
        """Generate features for a batch of images or a WSI."""
        if isinstance(obj, sf.WSI):
            grid = features_from_slide(self, obj, **kwargs)
            return np.ma.masked_where(grid == -99, grid)
        elif kwargs:
            raise ValueError(
                f"{self.__class__.__name__} does not accept keyword arguments "
                "when extracting features from a batch of images."
            )
        assert obj.dtype == torch.uint8
        obj = self.transform(obj)
        return self.model(obj)

    def dump_config(self):
        """Return a dictionary of configuration parameters.

        These configuration parameters can be used to reconstruct the
        feature extractor, using ``slideflow.model.build_feature_extractor()``.

        """
        cls_name = self.__class__.__name__
        return {
            'class': f'slideflow.model.extractors.plip.{cls_name}',
            'kwargs': {'center_crop': self._center_crop},
        }
