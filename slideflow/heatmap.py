import os
from collections import namedtuple
from typing import (TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple,
                    Union)

import numpy as np
import shapely.geometry as sg
from mpl_toolkits.axes_grid1.inset_locator import mark_inset, zoomed_inset_axes
from threading import Thread

import slideflow as sf
from slideflow import errors
from slideflow.slide import WSI
from slideflow.util import log

if TYPE_CHECKING:
    import matplotlib.pyplot as plt
    from matplotlib.axes import Axes
    try:
        import tensorflow as tf
    except ImportError:
        pass
    try:
        import torch
    except ImportError:
        pass


Inset = namedtuple("Inset", "x y zoom loc mark1 mark2 axes")


class Heatmap:
    """Generates a heatmap of predictions across a whole-slide image."""

    def __init__(
        self,
        slide: Union[str, WSI],
        model: str,
        stride_div: Optional[int] = None,
        batch_size: int = 32,
        num_threads: Optional[int] = None,
        num_processes: Optional[int] = None,
        img_format: str = 'auto',
        generate: bool = True,
        generator_kwargs: Optional[Dict[str, Any]] = None,
        device: Optional["torch.device"] = None,
        **wsi_kwargs
    ) -> None:
        """Initialize a heatmap from a path to a slide or a :class:``slideflow.WSI``.

        Examples
            Create a heatmap from a path to a slide.

                .. code-block:: python

                    model_path = 'path/to/saved_model'
                    heatmap = sf.Heatmap('slide.svs', model_path)

            Create a heatmap, with grayspace filtering disabled.

                .. code-block:: python

                    heatmap = sf.Heatmap(..., grayspace_fraction=1)

            Create a heatmap from a ``sf.WSI`` object.

                .. code-block:: python

                    # Load a slide
                    wsi = sf.WSI(tile_px=299, tile_um=302)

                    # Apply Otsu's thresholding to the slide,
                    # so heatmap is only generated on areas with tissue.
                    wsi.qc('otsu')

                    # Generate the heatmap
                    heatmap = sf.Heatmap(wsi, model_path)

        Args:
            slide (str): Path to slide.
            model (str): Path to Tensorflow or PyTorch model.
            stride_div (int, optional): Divisor for stride when convoluting
                across slide. Defaults to 2.
            roi_dir (str, optional): Directory in which slide ROI is contained.
                Defaults to None.
            rois (list, optional): List of paths to slide ROIs. Alternative to
                providing roi_dir. Defaults to None.
            roi_method (str): Either 'inside', 'outside', 'auto', or 'ignore'.
                Determines how ROIs are used to extract tiles.
                If 'inside' or 'outside', will extract tiles in/out of an ROI,
                and raise errors.MissingROIError if an ROI is not available.
                If 'auto', will extract tiles inside an ROI if available,
                and across the whole-slide if no ROI is found.
                If 'ignore', will extract tiles across the whole-slide
                regardless of whether an ROI is available.
                Defaults to 'auto'.
            batch_size (int, optional): Batch size for calculating predictions.
                Defaults to 32.
            num_threads (int, optional): Number of tile worker threads. Cannot
                supply both ``num_threads`` (uses thread pool) and
                ``num_processes`` (uses multiprocessing pool). Defaults to
                CPU core count.
            num_processes (int, optional): Number of child processes to spawn
                for multiprocessing pool. Defaults to None (does not use
                multiprocessing).
            enable_downsample (bool, optional): Enable the use of downsampled
                slide image layers. Defaults to True.
            img_format (str, optional): Image format (png, jpg) to use when
                extracting tiles from slide. Must match the image format
                the model was trained on. If 'auto', will use the format
                logged in the model params.json. Defaults to 'auto'.
            generate (bool): Generate the heatmap after initialization.
                If False, heatmap will need to be manually generated by
                calling :meth:``Heatmap.generate()``.
            generator_kwargs (dict, optional): Keyword arguments passed to
                the :meth:`slideflow.WSI.build_generator()`.
            device (torch.device, optional): PyTorch device. Defaults to
                initializing a new CUDA device.

        Keyword args:
            Any keyword argument accepted by :class:`slideflow.WSI`.
        """
        if num_processes is not None and num_threads is not None:
            raise ValueError("Invalid argument: cannot supply both "
                             "num_processes and num_threads")
        self.insets = []  # type: List[Inset]

        model_config = sf.util.get_model_config(model)
        self.uq = model_config['hp']['uq']
        if img_format == 'auto' and 'img_format' not in model_config:
            raise errors.HeatmapError(
                f"Unable to auto-detect image format from model at {model}. "
                "Manually set to png or jpg with Heatmap(img_format=...)")
        elif img_format == 'auto':
            self.img_format = model_config['img_format']
        else:
            self.img_format = img_format

        if sf.util.is_torch_model_path(model):
            int_kw = {'device': device}
        else:
            int_kw = {}

        if self.uq:
            self.interface = sf.model.UncertaintyInterface(model, **int_kw)  # type: ignore
        else:
            self.interface = sf.model.Features(  # type: ignore
                model,
                layers=None,
                include_preds=True,
                **int_kw)
        self.model_path = model
        self.num_threads = num_threads
        self.num_processes = num_processes
        self.batch_size = batch_size
        self.device = device
        self.tile_px = model_config['tile_px']
        self.tile_um = model_config['tile_um']
        self.num_classes = self.interface.num_classes
        self.num_features = self.interface.num_features
        self.num_uncertainty = self.interface.num_uncertainty
        self.predictions = None
        self.uncertainty = None

        if isinstance(slide, str):
            if stride_div is None:
                stride_div = 2

            self.slide_path = slide
            self.stride_div = stride_div
            try:
                self.slide = WSI(
                    self.slide_path,
                    self.tile_px,
                    self.tile_um,
                    self.stride_div,
                    **wsi_kwargs  # type: ignore
                )
            except errors.SlideLoadError:
                raise errors.HeatmapError(
                    f'Error loading slide {self.slide.name} for heatmap')
        elif isinstance(slide, WSI):

            if slide.tile_px != self.tile_px:
                raise ValueError(
                    "Slide tile_px ({}) does not match model ({})".format(
                        slide.tile_px, self.tile_px))
            if slide.tile_um != self.tile_um:
                raise ValueError(
                    "Slide tile_um ({}) does not match model ({})".format(
                        slide.tile_um, self.tile_um))
            if stride_div is not None:
                log.warn("slide is a WSI; ignoring supplied stride_div.")
            if wsi_kwargs:
                log.warn("WSI provided; ignoring keyword arguments: "
                         ", ".join(list(wsi_kwargs.keys())))

            self.slide_path = slide.path
            self.slide = slide
            self.stride_div = slide.stride_div
        else:
            raise ValueError(f"Unrecognized value {slide} for argument slide")

        if generate:
            if generator_kwargs is None:
                generator_kwargs = {}
            self.generate(**generator_kwargs)
        elif generator_kwargs:
            log.warn("Heatmap generate=False, ignoring generator_kwargs ("
                     f"{generator_kwargs})")

    @staticmethod
    def _prepare_ax(ax: Optional["Axes"] = None) -> "Axes":
        """Creates matplotlib figure and axis if one is not supplied,
        otherwise clears the axis contents.

        Args:
            ax (matplotlib.axes.Axes): Figure axis. If not supplied,
                will create a new figure and axis. Otherwise, clears axis
                contents. Defaults to None.

        Returns:
            matplotlib.axes.Axes: Figure axes.
        """
        import matplotlib.pyplot as plt
        if ax is None:
            fig = plt.figure(figsize=(18, 16))
            ax = fig.add_subplot(111)
            fig.subplots_adjust(bottom=0.25, top=0.95)
        else:
            ax.clear()
        return ax

    def generate(
        self,
        asynchronous: bool = False,
        **kwargs
    ) -> Optional[Tuple[np.ndarray, Thread]]:
        """Manually generate the heatmap.

        This function is automatically called when creating the heatmap if the
        heatmap was initialized with ``generate=True`` (default behavior).

        Args:
            asynchronous (bool, optional): Generate heatmap in a separate thread,
                returning the numpy array which is updated in realtime with
                heatmap predictions and the heatmap thread. Defaults to False,
                returning None.

        Returns:
            ``None`` if ``threaded=False``, otherwise returns a tuple containing

                    **grid**: Numpy array containing updated in realtime
                    with heatmap predictions as they are calculated.

                    **Thread**: Thread in which heatmap is generated.
        """

        # Load the slide
        def _generate(grid=None):
            out = self.interface(
                self.slide,
                num_threads=self.num_threads,
                num_processes=self.num_processes,
                batch_size=self.batch_size,
                img_format=self.img_format,
                dtype=np.float32,
                grid=grid,
                **kwargs
            )
            if self.uq:
                self.predictions = out[:, :, :-(self.num_uncertainty)]
                self.uncertainty = out[:, :, -(self.num_uncertainty):]
            else:
                self.predictions = out
                self.uncertainty = None
                log.info(f"Heatmap complete for [green]{self.slide.name}")

        if asynchronous:
            it = self.interface
            grid = np.ones((
                    self.slide.grid.shape[1],
                    self.slide.grid.shape[0],
                    it.num_features + it.num_classes + it.num_uncertainty),
                dtype=np.float32)
            grid *= -1
            heatmap_thread = Thread(target=_generate, args=(grid,))
            heatmap_thread.start()
            return grid, heatmap_thread
        else:
            _generate()
            return None

    def _format_ax(
        self,
        ax: "Axes",
        thumb_size: Tuple[int, int],
        show_roi: bool = True,
        **kwargs
    ) -> None:
        """Formats matplotlib axis in preparation for heatmap plotting.

        Args:
            ax (matplotlib.axes.Axes): Figure axis.
            show_roi (bool, optional): Include ROI on heatmap. Defaults to True.
        """
        ax.tick_params(
            axis='x',
            top=True,
            labeltop=True,
            bottom=False,
            labelbottom=False
        )
        # Plot ROIs
        if show_roi:
            roi_scale = self.slide.dimensions[0] / thumb_size[0]
            annPolys = [
                sg.Polygon(annotation.scaled_area(roi_scale))
                for annotation in self.slide.rois
            ]
            for poly in annPolys:
                x, y = poly.exterior.xy
                ax.plot(x, y, zorder=20, **kwargs)

    def add_inset(
        self,
        x: Tuple[int, int],
        y: Tuple[int, int],
        zoom: int = 5,
        loc: int = 1,
        mark1: int = 2,
        mark2: int = 4,
        axes: bool = True
    ) -> Inset:
        """Adds a zoom inset to the heatmap."""
        _inset = Inset(
                x=x,
                y=y,
                zoom=zoom,
                loc=loc,
                mark1=mark1,
                mark2=mark2,
                axes=axes
        )
        self.insets += [_inset]
        return _inset

    def clear_insets(self) -> None:
        """Removes zoom insets."""
        self.insets = []

    def load(self, path: str) -> None:
        """Load heatmap predictions and uncertainty from .npz file.

        This function is an alias for :meth:`slideflow.Heatmap.load_npz()`.

        Args:
            path (str, optional): Source .npz file. Must have 'predictions' key
                and optionally 'uncertainty'.

        Returns:
            None
        """
        self.load_npz(path)

    def load_npz(self, path: str) -> None:
        """Load heatmap predictions and uncertainty from .npz file.

        Loads predictions from ``'predictions'`` in .npz file, and uncertainty from
        ``'uncertainty'`` if present, as generated from
        :meth:`slideflow.Heatmap.save_npz()``. This function is the same as
        calling ``heatmap.load()``.

        Args:
            path (str, optional): Source .npz file. Must have 'predictions' key
                and optionally 'uncertainty'.

        Returns:
            None
        """
        npzfile = np.load(path)
        if ('predictions' not in npzfile) and ('logits' in npzfile):
            log.warn("Loading predictions from 'logits' key.")
            self.predictions = npzfile['logits']
        else:
            self.predictions = npzfile['predictions']
        if 'uncertainty' in npzfile:
            self.uncertainty = npzfile['uncertainty']

    def plot_thumbnail(
        self,
        show_roi: bool = False,
        roi_color: str = 'k',
        linewidth: int = 5,
        width: Optional[int] = None,
        mpp: Optional[float] = None,
        ax: Optional["Axes"] = None,
    ) -> "plt.image.AxesImage":
        """Plot a thumbnail of the slide, with or without ROI.

        Args:
            show_roi (bool, optional): Overlay ROIs onto heatmap image.
                Defaults to True.
            roi_color (str): ROI line color. Defaults to 'k' (black).
            linewidth (int): Width of ROI line. Defaults to 5.
            ax (matplotlib.axes.Axes, optional): Figure axis. If not supplied,
                will prepare a new figure axis.

        Returns:
            plt.image.AxesImage: Result from ax.imshow().
        """
        ax = self._prepare_ax(ax)
        if width is None and mpp is None:
            width = 2048
        thumb = self.slide.thumb(width=width, mpp=mpp)
        self._format_ax(
            ax,
            thumb_size=thumb.size,
            show_roi=show_roi,
            color=roi_color,
            linewidth=linewidth,
        )
        imshow_thumb = ax.imshow(thumb, zorder=0)

        for inset in self.insets:
            axins = zoomed_inset_axes(ax, inset.zoom, loc=inset.loc)
            axins.imshow(thumb)
            axins.set_xlim(inset.x[0], inset.x[1])
            axins.set_ylim(inset.y[0], inset.y[1])
            mark_inset(
                ax,
                axins,
                loc1=inset.mark1,
                loc2=inset.mark2,
                fc='none',
                ec='0',
                zorder=100
            )
            if not inset.axes:
                axins.get_xaxis().set_ticks([])
                axins.get_yaxis().set_ticks([])

        return imshow_thumb

    def plot_with_logit_cmap(
        self,
        logit_cmap: Union[Callable, Dict],
        interpolation: str = 'none',
        ax: Optional["Axes"] = None,
        **thumb_kwargs,
    ) -> None:
        """Plot a heatmap using a specified logit colormap.

        Args:
            logit_cmap (obj, optional): Either function or a dictionary use to
                create heatmap colormap. Each image tile will generate a list
                of predictions of length O, where O is the number of outcomes.
                If logit_cmap is a function, then the logit prediction list
                will be passed to the function, and the function is expected
                to return [R, G, B] values for display. If logit_cmap is a
                dictionary, it should map 'r', 'g', and 'b' to indices; the
                prediction for these outcome indices will be mapped to the RGB
                colors. Thus, the corresponding color will only reflect up to
                three outcomes. Example mapping prediction for outcome 0 to the
                red colorspace, 3 to green, etc: {'r': 0, 'g': 3, 'b': 1}
            interpolation (str, optional): Interpolation strategy to use for
                smoothing heatmap. Defaults to 'none'.
            ax (matplotlib.axes.Axes, optional): Figure axis. If not supplied,
                will prepare a new figure axis.

        Keyword args:
            show_roi (bool, optional): Overlay ROIs onto heatmap image.
                Defaults to True.
            roi_color (str): ROI line color. Defaults to 'k' (black).
            linewidth (int): Width of ROI line. Defaults to 5.
        """
        ax = self._prepare_ax(ax)
        implot = self.plot_thumbnail(ax=ax, **thumb_kwargs)
        ax.set_facecolor("black")
        if callable(logit_cmap):
            map_logit = logit_cmap
        else:
            # Make heatmap with specific logit predictions mapped
            # to r, g, and b
            def map_logit(logit):
                return (logit[logit_cmap['r']],
                        logit[logit_cmap['g']],
                        logit[logit_cmap['b']])
        ax.imshow(
            [[map_logit(logit) for logit in row] for row in self.predictions],
            extent=implot.get_extent(),
            interpolation=interpolation,
            zorder=10
        )

    def plot_uncertainty(
        self,
        heatmap_alpha: float = 0.6,
        cmap: str = 'coolwarm',
        interpolation: str = 'none',
        ax: Optional["Axes"] = None,
        **thumb_kwargs
    ):
        """Plot heatmap of uncertainty.

        Args:
            heatmap_alpha (float, optional): Alpha of heatmap overlay.
                Defaults to 0.6.
            cmap (str, optional): Matplotlib heatmap colormap.
                Defaults to 'coolwarm'.
            interpolation (str, optional): Interpolation strategy to use for
                smoothing heatmap. Defaults to 'none'.
            ax (matplotlib.axes.Axes, optional): Figure axis. If not supplied,
                will prepare a new figure axis.

        Keyword args:
            show_roi (bool, optional): Overlay ROIs onto heatmap image.
                Defaults to True.
            roi_color (str): ROI line color. Defaults to 'k' (black).
            linewidth (int): Width of ROI line. Defaults to 5.
        """
        import matplotlib.colors as mcol

        ax = self._prepare_ax(ax)
        implot = self.plot_thumbnail(ax=ax, **thumb_kwargs)
        if heatmap_alpha == 1:
            implot.set_alpha(0)
        uqnorm = mcol.TwoSlopeNorm(
            vmin=0,
            vcenter=self.uncertainty.max()/2,
            vmax=self.uncertainty.max()
        )
        masked_uncertainty = np.ma.masked_where(
            self.uncertainty == -1,
            self.uncertainty
        )
        ax.imshow(
            masked_uncertainty,
            norm=uqnorm,
            extent=implot.get_extent(),
            cmap=cmap,
            alpha=heatmap_alpha,
            interpolation=interpolation,
            zorder=10
        )

    def plot(
        self,
        class_idx: int,
        heatmap_alpha: float = 0.6,
        cmap: str = 'coolwarm',
        interpolation: str = 'none',
        vmin: float = 0,
        vmax: float = 1,
        vcenter: float = 0.5,
        ax: Optional["Axes"] = None,
        **thumb_kwargs
    ) -> None:
        """Plot a predictive heatmap.

        If in a Jupyter notebook, the heatmap will be displayed in the cell
        output. If running via script or shell, the heatmap can then be
        shown on screen using matplotlib ``plt.show()``:

        .. code-block::

            import slideflow as sf
            import matplotlib.pyplot as plt

            heatmap = sf.Heatmap(...)
            heatmap.plot()
            plt.show()

        Args:
            class_idx (int): Class index to plot.
            heatmap_alpha (float, optional): Alpha of heatmap overlay.
                Defaults to 0.6.
            show_roi (bool, optional): Overlay ROIs onto heatmap image.
                Defaults to True.
            cmap (str, optional): Matplotlib heatmap colormap.
                Defaults to 'coolwarm'.
            interpolation (str, optional): Interpolation strategy to use for
                smoothing heatmap. Defaults to 'none'.
            vmin (float): Minimimum value to display on heatmap.
                Defaults to 0.
            vcenter (float): Center value for color display on heatmap.
                Defaults to 0.5.
            vmax (float): Maximum value to display on heatmap.
                Defaults to 1.
            ax (matplotlib.axes.Axes, optional): Figure axis. If not supplied,
                will prepare a new figure axis.

        Keyword args:
            show_roi (bool, optional): Overlay ROIs onto heatmap image.
                Defaults to True.
            roi_color (str): ROI line color. Defaults to 'k' (black).
            linewidth (int): Width of ROI line. Defaults to 5.
        """
        import matplotlib.colors as mcol

        if self.predictions is None:
            raise errors.HeatmapError(
                "Cannot plot Heatmap which is not yet generated; generate with "
                "either heatmap.generate() or Heatmap(..., generate=True)"
            )

        ax = self._prepare_ax(ax)
        implot = self.plot_thumbnail(ax=ax, **thumb_kwargs)
        if heatmap_alpha == 1:
            implot.set_alpha(0)
        ax.set_facecolor("black")
        divnorm = mcol.TwoSlopeNorm(
            vmin=vmin,
            vcenter=vcenter,
            vmax=vmax
        )
        masked_arr = np.ma.masked_where(
            self.predictions[:, :, class_idx] == -1,
            self.predictions[:, :, class_idx]
        )
        ax.imshow(
            masked_arr,
            norm=divnorm,
            extent=implot.get_extent(),
            cmap=cmap,
            alpha=heatmap_alpha,
            interpolation=interpolation,
            zorder=10
        )

    def save_npz(self, path: Optional[str] = None) -> str:
        """Save heatmap predictions and uncertainty in .npz format.

        Saves heatmap predictions to ``'predictions'`` in the .npz file. If uncertainty
        was calculated, this is saved to ``'uncertainty'``. A Heatmap instance can
        load a saved .npz file with :meth:`slideflow.Heatmap.load()`.

        Args:
            path (str, optional): Destination filename for .npz file. Defaults
                to {slidename}.npz

        Returns:
            str: Path to .npz file.
        """
        if path is None:
            path = f'{self.slide.name}.npz'
        np_kwargs = dict(predictions=self.predictions)
        if self.uq:
            np_kwargs['uncertainty'] = self.uncertainty
        np.savez(path, **np_kwargs)
        return path

    def save(
        self,
        outdir: str,
        show_roi: bool = True,
        interpolation: str = 'none',
        logit_cmap: Optional[Union[Callable, Dict]] = None,
        roi_color: str = 'k',
        linewidth: int = 5,
        **kwargs
    ) -> None:
        """Saves calculated predictions as heatmap overlays.

        Args:
            outdir (str): Path to directory in which to save heatmap images.
            show_roi (bool, optional): Overlay ROIs onto heatmap image.
                Defaults to True.
            interpolation (str, optional): Interpolation strategy to use for
                smoothing heatmap. Defaults to 'none'.
            logit_cmap (obj, optional): Either function or a dictionary use to
                create heatmap colormap. Each image tile will generate a list
                of predictions of length O, where O is the number of outcomes.
                If logit_cmap is a function, then the logit prediction list
                will be passed to the function, and the function is expected
                to return [R, G, B] values for display. If logit_cmap is a
                dictionary, it should map 'r', 'g', and 'b' to indices; the
                prediction for these outcome indices will be mapped to the RGB
                colors. Thus, the corresponding color will only reflect up to
                three outcomes. Example mapping prediction for outcome 0 to the
                red colorspace, 3 to green, etc: {'r': 0, 'g': 3, 'b': 1}
            roi_color (str): ROI line color. Defaults to 'k' (black).
            linewidth (int): Width of ROI line. Defaults to 5.

        Keyword args:
            cmap (str, optional): Matplotlib heatmap colormap.
                Defaults to 'coolwarm'.
            vmin (float): Minimimum value to display on heatmap.
                Defaults to 0.
            vcenter (float): Center value for color display on heatmap.
                Defaults to 0.5.
            vmax (float): Maximum value to display on heatmap.
                Defaults to 1.

        """
        import matplotlib.pyplot as plt

        if self.predictions is None:
            raise errors.HeatmapError(
                "Cannot plot Heatmap which is not yet generated; generate with "
                "either heatmap.generate() or Heatmap(..., generate=True)"
            )

        # Save heatmaps in .npz format
        self.save_npz(os.path.join(outdir, f'{self.slide.name}.npz'))

        def _savefig(label, bbox_inches='tight', **kwargs):
            plt.savefig(
                os.path.join(outdir, f'{self.slide.name}-{label}.png'),
                bbox_inches=bbox_inches,
                **kwargs
            )

        log.info('Saving base figures...')

        # Prepare matplotlib figure
        ax = self._prepare_ax()

        thumb_kwargs = dict(roi_color=roi_color, linewidth=linewidth)

        # Save base thumbnail as separate figure
        self.plot_thumbnail(show_roi=False, ax=ax, **thumb_kwargs)  # type: ignore
        _savefig('raw')

        # Save thumbnail + ROI as separate figure
        self.plot_thumbnail(show_roi=True, ax=ax, **thumb_kwargs)  # type: ignore
        _savefig('raw+roi')

        if logit_cmap:
            self.plot_with_logit_cmap(logit_cmap, show_roi=show_roi, ax=ax)
            _savefig('custom')
        else:
            heatmap_kwargs = dict(
                show_roi=show_roi,
                interpolation=interpolation,
                **kwargs
            )
            save_kwargs = dict(
                bbox_inches='tight',
                facecolor=ax.get_facecolor(),
                edgecolor='none'
            )
            # Make heatmap plots and sliders for each outcome category
            for i in range(self.num_classes):
                log.info(f'Making {i+1}/{self.num_classes}...')
                self.plot(i, heatmap_alpha=0.6, ax=ax, **heatmap_kwargs)
                _savefig(str(i), **save_kwargs)

                self.plot(i, heatmap_alpha=1, ax=ax, **heatmap_kwargs)
                _savefig(f'{i}-solid', **save_kwargs)

            # Uncertainty map
            if self.uq:
                log.info('Making uncertainty heatmap...')
                self.plot_uncertainty(heatmap_alpha=0.6, ax=ax, **heatmap_kwargs)
                _savefig('UQ', **save_kwargs)

                self.plot_uncertainty(heatmap_alpha=1, ax=ax, **heatmap_kwargs)
                _savefig('UQ-solid', **save_kwargs)

        plt.close()
        log.info(f'Saved heatmaps for [green]{self.slide.name}')

    def view(self):
        """Load the Heatmap into Slideflow Studio for interactive view.

        See :ref:`studio` for more information.

        """
        from slideflow.studio import Studio

        studio = Studio()
        studio.load_slide(self.slide.path)
        studio.load_model(self.model_path)
        studio.load_heatmap(self)
        studio.run()

class ModelHeatmap(Heatmap):

    def __init__(
        self,
        slide: Union[str, WSI],
        model: Union[str, "torch.nn.Module", "tf.keras.Model"],
        img_format: str,
        tile_px: Optional[int] = None,
        tile_um: Optional[int] = None,
        stride_div: Optional[int] = None,
        batch_size: int = 32,
        num_threads: Optional[int] = None,
        num_processes: Optional[int] = None,
        generate: bool = True,
        normalizer: Optional[sf.norm.StainNormalizer] = None,
        uq: bool = False,
        generator_kwargs: Optional[Dict[str, Any]] = None,
        **wsi_kwargs
    ):
        """Convolutes across a whole slide, calculating predictions and saving
        predictions internally for later use.

        Args:
            slide (str): Path to slide.
            model (str): Path to Tensorflow or PyTorch model.
            stride_div (int, optional): Divisor for stride when convoluting
                across slide. Defaults to 2.
            roi_dir (str, optional): Directory in which slide ROI is contained.
                Defaults to None.
            rois (list, optional): List of paths to slide ROIs. Alternative to
                providing roi_dir. Defaults to None.
            roi_method (str): Either 'inside', 'outside', 'auto', or 'ignore'.
                Determines how ROIs are used to extract tiles.
                If 'inside' or 'outside', will extract tiles in/out of an ROI,
                and raise errors.MissingROIError if an ROI is not available.
                If 'auto', will extract tiles inside an ROI if available,
                and across the whole-slide if no ROI is found.
                If 'ignore', will extract tiles across the whole-slide
                regardless of whether an ROI is available.
                Defaults to 'auto'.
            batch_size (int, optional): Batch size for calculating predictions.
                Defaults to 32.
            num_threads (int, optional): Number of tile worker threads. Cannot
                supply both ``num_threads`` (uses thread pool) and
                ``num_processes`` (uses multiprocessing pool). Defaults to
                CPU core count.
            num_processes (int, optional): Number of child processes to spawn
                for multiprocessing pool. Defaults to None (does not use
                multiprocessing).
            enable_downsample (bool, optional): Enable the use of downsampled
                slide image layers. Defaults to True.
            img_format (str, optional): Image format (png, jpg) to use when
                extracting tiles from slide. Must match the image format
                the model was trained on. If 'auto', will use the format
                logged in the model params.json.
            generate (bool): Generate the heatmap after initialization.
                If False, heatmap will need to be manually generated by
                calling :meth:``Heatmap.generate()``.
        """
        if num_processes is not None and num_threads is not None:
            raise ValueError("Invalid argument: cannot supply both "
                             "num_processes and num_threads")
        self.uq = uq
        self.img_format = img_format
        self.num_threads = num_threads
        self.num_processes = num_processes
        self.batch_size = batch_size
        self.insets = []  # type: List[Inset]
        if generator_kwargs is None:
            generator_kwargs = {}

        if isinstance(slide, str):

            if tile_px is None:
                raise ValueError("If slide is a path, must supply tile_px.")
            if tile_um is None:
                raise ValueError("If slide is a path, must supply tile_um.")
            if stride_div is None:
                stride_div = 2

            self.slide_path = slide
            self.tile_px = tile_px
            self.tile_um = tile_um
            self.stride_div = stride_div
            try:
                self.slide = WSI(
                    self.slide_path,
                    self.tile_px,
                    self.tile_um,
                    self.stride_div,
                    **wsi_kwargs  # type: ignore
                )
            except errors.SlideLoadError:
                raise errors.HeatmapError(
                    f'Error loading slide {self.slide.name} for heatmap')
        elif isinstance(slide, WSI):

            if tile_px is not None:
                log.warn("slide is a WSI; ignoring supplied tile_px.")
            if tile_um is not None:
                log.warn("slide is a WSI; ignoring supplied tile_um.")
            if stride_div is not None:
                log.warn("slide is a WSI; ignoring supplied stride_div.")
            if wsi_kwargs:
                log.warn("WSI provided; ignoring keyword arguments: " +
                         ", ".join(list(wsi_kwargs.keys())))

            self.slide_path = slide.path
            self.slide = slide
            self.tile_px = slide.tile_px
            self.tile_um = slide.tile_um
            self.stride_div = slide.stride_div
        else:
            raise ValueError(f"Unrecognized value {slide} for argument slide")

        if uq:
            interface_class = sf.model.UncertaintyInterface  # type: ignore
            interface_kw = {}  # type: Dict[str, Any]
        elif sf.util.model_backend(model) == 'tensorflow':
            import slideflow.model.tensorflow
            interface_class = sf.model.tensorflow.Features  # type: ignore
            interface_kw = dict(include_preds=True)
        elif sf.util.model_backend(model) == 'torch':
            import slideflow.model.torch
            interface_class = sf.model.torch.Features  # type: ignore
            interface_kw = dict(include_preds=True, tile_px=self.tile_px)
        else:
            raise ValueError(f"Unable to interpret model {model}")

        if isinstance(model, str):
            self.interface = interface_class(
                model,
                layers=None,
                **interface_kw)
        else:
            self.interface = interface_class.from_model(
                model,
                layers=None,
                wsi_normalizer=normalizer,
                **interface_kw)
        self.num_classes = self.interface.num_classes
        self.num_features = self.interface.num_features
        self.num_uncertainty = self.interface.num_uncertainty
        self.predictions = None
        self.uncertainty = None
        self.model_path = None

        if generate:
            self.generate(**generator_kwargs)
        elif generator_kwargs:
            log.warn("Heatmap generate=False, ignoring generator_kwargs ("
                     f"{generator_kwargs})")

    def view(self):
        raise NotImplementedError