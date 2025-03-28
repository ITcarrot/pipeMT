from typing import *
import traceback

import torch.nn as nn
from torch.distributed.pipelining import pipeline

from pipeMT.async_handle import pipeMTAsyncHandle
from pipeMT.batch import Batch
from pipeMT.device import *
from pipeMT.parameter import preprocess_param
from pipeMT.scheduler import model_enqueue
from pipeMT.utils import get_model_size

if TYPE_CHECKING:
    import torch.fx as fx
    from torch.distributed.pipelining import SplitPoint

class pipeMT(nn.Module):
    def __init__(self,
                 model: Union[nn.Module, Iterable[nn.Module]],
                 split_spec: Optional[Dict[str, 'SplitPoint']] = None,
                 split_policy: Optional[Callable[['fx.GraphModule'], 'fx.GraphModule']] = None):
        super().__init__()
        filename, lineno, _, _ = traceback.extract_stack()[-2]
        self.name = f'{filename.split('/')[-1]}:{lineno}'
        
        self.layer_size = []
        if isinstance(model, nn.Sequential):
            self.model = model
            self.layers = list(model)
            self.num_layers = len(self.layers)
            self.init_layer_size()
            self.model_size = sum(self.layer_size)
            self.require_spliting = False
        elif isinstance(model, nn.Module):
            self.model = model
            self.model_size = get_model_size(model)
            self.require_spliting = True
            self.split_spec = split_spec
            self.split_policy = split_policy
        else:
            raise TypeError('input model should be torch.nn.Module or torch.nn.Sequential')
        preprocess_param(self.model)

    def __getattr__(self, name: str):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.model, name)

    def init_layer_size(self):
        for layer in self.layers:
            self.layer_size.append(get_model_size(layer))
    
    def split_model(self,
                    mb_args: Tuple[Any, ...] = tuple(),
                    mb_kwargs: Optional[Dict[str, Any]] = None,
                    split_spec: Optional[Dict[str, 'SplitPoint']] = None,
                    split_policy: Optional[Callable[['fx.GraphModule'], 'fx.GraphModule']] = None):
        # This function is thread un-safe, be aware of pytorch export racing
        self.require_spliting = False
        split_spec = self.split_spec if split_spec is None else split_spec
        split_policy = self.split_policy if split_policy is None else split_policy
        pipe = pipeline(self.model, mb_args, mb_kwargs, split_spec, split_policy)
        self.layers = []
        self.num_layers = pipe.num_stages
        for i in range(pipe.num_stages):
            self.layers.append(pipe.get_stage_module(i))
        self.init_layer_size()

    def forward(self, *args,
                is_async: bool = False, require_grad: bool = torch.is_grad_enabled(),
                output_device: torch.device = torch.device('cpu'),
                **kwargs):
        input = Batch(*args, **kwargs)
        result_handle = pipeMTAsyncHandle(self, input, require_grad, output_device)
        model_enqueue(result_handle)
        return result_handle if is_async else result_handle.get_result()
