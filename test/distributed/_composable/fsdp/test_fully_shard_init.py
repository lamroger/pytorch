# Owner(s): ["oncall: distributed"]

import itertools
import unittest
from typing import List

import torch
import torch.nn as nn
from torch.distributed._composable import replicate
from torch.distributed._composable.fsdp import fully_shard
from torch.distributed._composable.fsdp._fsdp_init import (
    _get_managed_modules,
    _get_managed_states,
)
from torch.distributed._tensor import DTensor
from torch.distributed.device_mesh import init_device_mesh
from torch.distributed.tensor.parallel import (
    ColwiseParallel,
    parallelize_module,
    RowwiseParallel,
)
from torch.testing._internal.common_cuda import TEST_CUDA
from torch.testing._internal.common_fsdp import FSDPTestMultiThread, MLP
from torch.testing._internal.common_utils import run_tests


class TestFullyShardDeviceTensor(FSDPTestMultiThread):
    """Tests that tensor parameters are moved to the expected device."""

    @property
    def world_size(self) -> int:
        return 1

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_move_states_to_device_tensor(self):
        model = MLP(8, torch.device("cpu"), with_buffer=True)
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            self.assertEqual(tensor.device, torch.device("cpu"))
        fully_shard(model)
        cuda_device = torch.device("cuda", torch.cuda.current_device())
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            self.assertEqual(tensor.device, cuda_device)


class TestFullyShardDeviceDTensor(FSDPTestMultiThread):
    """Tests that DTensor parameters are moved to the expected device."""

    @property
    def world_size(self) -> int:
        return 4

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_move_states_to_device_dtensor_valid(self):
        assert self.world_size >= 4, f"{self.world_size}"
        dp_size = 2
        global_mesh = init_device_mesh(
            "cuda", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        dp_mesh = global_mesh["dp"]
        tp_mesh = global_mesh["tp"]
        model = MLP(8, torch.device("cpu"), with_buffer=True)
        parallelize_module(
            model,
            tp_mesh,
            {"in_proj": ColwiseParallel(), "out_proj": RowwiseParallel()},
        )
        cuda_device = torch.device("cuda", torch.cuda.current_device())
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            if isinstance(tensor, DTensor):
                # DTensor constructor moves to the mesh's device
                self.assertEqual(tensor.device, cuda_device)
                self.assertEqual(tensor._local_tensor.device, cuda_device)
            else:
                self.assertEqual(tensor.device, torch.device("cpu"))
        fully_shard(model, mesh=dp_mesh)
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            self.assertEqual(tensor.device, cuda_device)
            if isinstance(tensor, DTensor):
                self.assertEqual(tensor._local_tensor.device, cuda_device)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_move_states_to_device_dtensor_invalid(self):
        assert self.world_size >= 4, f"{self.world_size}"
        dp_size = 2
        global_cuda_mesh = init_device_mesh(
            "cuda", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        global_cpu_mesh = init_device_mesh(
            "cpu", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        dp_mesh = global_cuda_mesh["dp"]
        tp_mesh = global_cpu_mesh["tp"]  # mismatched meshes!
        model = MLP(8, torch.device("cpu"), with_buffer=True)
        parallelize_module(
            model,
            tp_mesh,
            {"in_proj": ColwiseParallel(), "out_proj": RowwiseParallel()},
        )
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            self.assertEqual(tensor.device, torch.device("cpu"))
            if isinstance(tensor, DTensor):
                self.assertEqual(tensor._local_tensor.device, torch.device("cpu"))
        regex = r"Requires DTensor to have mesh of the same type as the FSDP mesh but got cpu for DTensor and cuda for FSDP"
        with self.assertRaisesRegex(ValueError, regex):
            fully_shard(model, mesh=dp_mesh)


class TestFullyShardMeshArg(FSDPTestMultiThread):
    """Tests the ``mesh`` argument."""

    @property
    def world_size(self) -> int:
        return 2

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_invalid_mesh_ndim(self):
        mesh = init_device_mesh("cuda", (self.world_size, 1, 1))
        model = MLP(8)
        regex = r"fully\_shard expects a 1D or 2D DeviceMesh but got DeviceMesh\(\[\[\[0\]\], \[\[1\]\]\]\)"
        with self.assertRaisesRegex(ValueError, regex):
            fully_shard(model, mesh=mesh)


class TestFullyShardManagedModulesAndStates(FSDPTestMultiThread):
    """Tests getting the managed modules/states for a ``fully_shard`` module."""

    @property
    def world_size(self) -> int:
        return 1

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_single(self):
        model = MLP(8)
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules(model)
        expected_managed_modules = list(model.modules())
        self._check_managed_modules(managed_modules, expected_managed_modules)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_nested(self):
        model = nn.Sequential(*[MLP(8) for _ in range(2)])
        fully_shard(model[0])
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules(model)
        expected_managed_modules = list(model[1].modules()) + [model]
        self._check_managed_modules(managed_modules, expected_managed_modules)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_nested_fully_shard_and_replicate(self):
        model = nn.Sequential(*[MLP(8) for _ in range(3)])
        replicate(model[0])
        fully_shard(model[2])
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules(model)
        expected_managed_modules = list(model[1].modules()) + [model]
        self._check_managed_modules(managed_modules, expected_managed_modules)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_modules_duplicate(self):
        mlp = MLP(8)
        model = nn.Sequential(mlp, mlp)  # duplicate MLP
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules(model)
        # Check that the duplicate module is only counted once
        expected_managed_modules = list(mlp.modules()) + [model]
        self._check_managed_modules(managed_modules, expected_managed_modules)

    def _check_managed_modules(
        self,
        managed_modules: List[nn.Module],
        expected_managed_modules: List[nn.Module],
    ):
        self.assertEqual(len(managed_modules), len(expected_managed_modules))
        # Check set comparison since we do not require anything about the order
        self.assertEqual(set(managed_modules), set(expected_managed_modules))

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_states_shared_params_and_buffers(self):
        model = nn.Sequential(*[MLP(8, with_buffer=True) for _ in range(3)])
        model[0].in_proj.weight = model[1].in_proj.weight
        model[2].in_proj.weight = model[1].in_proj.weight
        model[1].buffer = model[2].buffer
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules(model)
        params, buffers = _get_managed_states(managed_modules)
        expected_params = list(model.parameters())  # de-dups shared
        expected_buffers = list(model.buffers())  # de-dups shared
        self._check_managed_states(params, buffers, expected_params, expected_buffers)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_managed_states_nested_fully_shard(self):
        model = nn.Sequential(*[MLP(8, with_buffer=True) for _ in range(2)])
        fully_shard(model[0])
        # Assume calling `fully_shard` on `model`
        managed_modules = _get_managed_modules(model)
        params, buffers = _get_managed_states(managed_modules)
        expected_params = list(model[1].parameters())
        expected_buffers = list(model[1].buffers())
        self._check_managed_states(params, buffers, expected_params, expected_buffers)

    def _check_managed_states(
        self,
        managed_params: List[nn.Parameter],
        managed_buffers: List[torch.Tensor],
        expected_managed_params: List[nn.Parameter],
        expected_managed_buffers: List[torch.Tensor],
    ):
        self.assertEqual(len(managed_params), len(expected_managed_params))
        self.assertEqual(len(managed_buffers), len(expected_managed_buffers))
        self.assertEqual(set(managed_params), set(expected_managed_params))
        self.assertEqual(set(managed_buffers), set(expected_managed_buffers))


if __name__ == "__main__":
    run_tests()