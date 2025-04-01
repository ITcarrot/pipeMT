from typing import *
import threading

import torch
from torch.utils._pytree import TreeSpec, tree_unflatten

from pipeMT.transfer import PinnedUpload

if TYPE_CHECKING:
    from pipeMT.pipeMT import pipeMT
    from pipeMT.batch import Batch

class pipeMTAsyncHandle:
    flatten_states: List[List[Union[Any, torch.Tensor]]]
    flatten_specs: List[TreeSpec]
    transfer_events: List[Tuple[Sequence[torch.cuda.Event], Sequence[Optional[torch.cuda.Event]]]]
    
    def __init__(self, model: 'pipeMT', input: 'Batch', require_grad: bool, output_device: torch.device):
        self.model = model
        self.input = input
        self.require_grad = require_grad
        self.output_device = output_device
        self.result_used = False
        
        self.lock = threading.Lock()
        self.cur_layer = 0 # write only at scheduler thread
        self.prefetch_layer = 0 # write only at scheduler or device monitor thread
        self.workload_to_proccess = 0. # write only at user thread
        self.workload_processed = 0. # write only at scheduler thread
        self.progress_sem: List[threading.Semaphore] = []
        
        self.result = None
        self.all_launched = threading.Event()
        
        self.mark_workload_to_proccess(model.model_workload, set())
    
    def mark_workload_to_proccess(self, workload: float, visited_handle: Set[int]):
        if self.is_ready() or id(self) in visited_handle:
            return
        visited_handle.add(id(self))
        self.workload_to_proccess += workload
        for handle in self.input.input_handles:
            handle.mark_workload_to_proccess(workload, visited_handle)

    def is_ready(self) -> bool:
        return self.all_launched.is_set()

    def flatten_input(self):
        self.flatten_states, self.transfer_events, self.flatten_specs \
            = self.input.flatten()
    
    def init_sem(self):
        self.progress_sem = [threading.Semaphore(self.input.num_microbatch if i == 0 else 0)
                             for i in range(self.model.num_layers)]

    def get_result(self) -> Any:
        from pipeMT.device import device_tag_detach
        import pipeMT.scheduler
        if self.result is None:
            self.all_launched.wait()
            device_tag_detach()
            pipeMT.scheduler.scheduling_size = 0
            if self.output_device != torch.device('cpu'):
                flatten_states_on_device = []
                for flatten_state, ((transfer_event,), _) in zip(self.flatten_states, self.transfer_events):
                    transfer_event.synchronize()
                    flatten_state_on_device = []
                    for arg in flatten_state:
                        if isinstance(arg, torch.Tensor):
                            flatten_state_on_device.append(PinnedUpload.apply(arg, self.output_device))
                        else:
                            flatten_state_on_device.append(arg)
                    flatten_states_on_device.append(flatten_state_on_device)
            else:
                flatten_states_on_device = self.flatten_states
                self.transfer_events[-1][0][0].synchronize()
            
            hidden_states = []
            for flatten_state, flatten_spec in zip(flatten_states_on_device, self.flatten_specs):
                hidden_states.append(tree_unflatten(flatten_state, flatten_spec))
            self.result = self.input.gather_result(hidden_states)
        return self.result
    