from .train import (
    train_mil,
    train_fastai,
    build_fastai_learner
)
from .eval import (
    eval_mil,
    predict_slide,
    save_mil_tile_predictions,
    get_mil_tile_predictions,
    generate_mil_features
)
from ._params import (
    mil_config,
    TrainerConfig,
    MILModelConfig
)
from .utils import load_model_weights
from ._registry import (
    list_trainers, list_models, is_trainer, is_model,
    get_trainer, get_model, get_model_config,
    register_trainer, register_model,
)

# -----------------------------------------------------------------------------

@register_trainer
def fastai():
    return TrainerConfig

# -----------------------------------------------------------------------------

@register_model
def attention_mil():
    from .models import Attention_MIL
    return Attention_MIL

@register_model
def mm_attention_mil():
    from .models import MultiModal_Attention_MIL
    return MultiModal_Attention_MIL

@register_model
def transmil():
    from .models import TransMIL
    return TransMIL

@register_model('bistro.transformer')
def bistro_transformer():
    from .models.bistro import Transformer
    return Transformer
