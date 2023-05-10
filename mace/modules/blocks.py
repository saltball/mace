###########################################################################################
# Elementary Block for Building O(3) Equivariant Higher Order Message Passing Neural Network
# Authors: Ilyes Batatia, Gregor Simm
# This program is distributed under the MIT License (see MIT.md)
###########################################################################################

from abc import abstractmethod
from typing import Callable, List, Optional, Tuple, Union

import numpy as np
import torch.nn.functional
from e3nn import nn, o3
from e3nn.util.jit import compile_mode

from mace.tools.torch_geometric.utils import to_dense_adj, to_dense_batch
from mace.tools.torch_tools import get_mask
from mace.tools.scatter import scatter_sum

from .irreps_tools import (
    linear_out_irreps,
    reshape_irreps,
    tp_out_irreps_with_instructions,
)
from .radial import BesselBasis, PolynomialCutoff
from .symmetric_contraction import SymmetricContraction


@compile_mode("script")
class LinearNodeEmbeddingBlock(torch.nn.Module):
    def __init__(self, irreps_in: o3.Irreps, irreps_out: o3.Irreps):
        super().__init__()
        self.linear = o3.Linear(irreps_in=irreps_in, irreps_out=irreps_out)

    def forward(
        self,
        node_attrs: torch.Tensor,
    ) -> torch.Tensor:  # [n_nodes, irreps]
        return self.linear(node_attrs)


@compile_mode("script")
class LinearReadoutBlock(torch.nn.Module):
    def __init__(self, irreps_in: o3.Irreps):
        super().__init__()
        self.linear = o3.Linear(irreps_in=irreps_in, irreps_out=o3.Irreps("0e"))

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # [n_nodes, irreps]  # [..., ]
        return self.linear(x)  # [n_nodes, 1]


@compile_mode("script")
class NonLinearReadoutBlock(torch.nn.Module):
    def __init__(
        self, irreps_in: o3.Irreps, MLP_irreps: o3.Irreps, gate: Optional[Callable]
    ):
        super().__init__()
        self.hidden_irreps = MLP_irreps
        self.linear_1 = o3.Linear(irreps_in=irreps_in, irreps_out=self.hidden_irreps)
        self.non_linearity = nn.Activation(irreps_in=self.hidden_irreps, acts=[gate])
        self.linear_2 = o3.Linear(
            irreps_in=self.hidden_irreps, irreps_out=o3.Irreps("0e")
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # [n_nodes, irreps]  # [..., ]
        x = self.non_linearity(self.linear_1(x))
        return self.linear_2(x)  # [n_nodes, 1]


@compile_mode("script")
class LinearDipoleReadoutBlock(torch.nn.Module):
    def __init__(self, irreps_in: o3.Irreps, dipole_only: bool = False):
        super().__init__()
        if dipole_only:
            self.irreps_out = o3.Irreps("1x1o")
        else:
            self.irreps_out = o3.Irreps("1x0e + 1x1o")
        self.linear = o3.Linear(irreps_in=irreps_in, irreps_out=self.irreps_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # [n_nodes, irreps]  # [..., ]
        return self.linear(x)  # [n_nodes, 1]


@compile_mode("script")
class NonLinearDipoleReadoutBlock(torch.nn.Module):
    def __init__(
        self,
        irreps_in: o3.Irreps,
        MLP_irreps: o3.Irreps,
        gate: Callable,
        dipole_only: bool = False,
    ):
        super().__init__()
        self.hidden_irreps = MLP_irreps
        if dipole_only:
            self.irreps_out = o3.Irreps("1x1o")
        else:
            self.irreps_out = o3.Irreps("1x0e + 1x1o")
        irreps_scalars = o3.Irreps(
            [(mul, ir) for mul, ir in MLP_irreps if ir.l == 0 and ir in self.irreps_out]
        )
        irreps_gated = o3.Irreps(
            [(mul, ir) for mul, ir in MLP_irreps if ir.l > 0 and ir in self.irreps_out]
        )
        irreps_gates = o3.Irreps([mul, "0e"] for mul, _ in irreps_gated)
        self.equivariant_nonlin = nn.Gate(
            irreps_scalars=irreps_scalars,
            act_scalars=[gate for _, ir in irreps_scalars],
            irreps_gates=irreps_gates,
            act_gates=[gate] * len(irreps_gates),
            irreps_gated=irreps_gated,
        )
        self.irreps_nonlin = self.equivariant_nonlin.irreps_in.simplify()
        self.linear_1 = o3.Linear(irreps_in=irreps_in, irreps_out=self.irreps_nonlin)
        self.linear_2 = o3.Linear(
            irreps_in=self.hidden_irreps, irreps_out=self.irreps_out
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # [n_nodes, irreps]  # [..., ]
        x = self.equivariant_nonlin(self.linear_1(x))
        return self.linear_2(x)  # [n_nodes, 1]


@compile_mode("script")
class AtomicEnergiesBlock(torch.nn.Module):
    atomic_energies: torch.Tensor

    def __init__(self, atomic_energies: Union[np.ndarray, torch.Tensor]):
        super().__init__()
        assert len(atomic_energies.shape) == 1

        self.register_buffer(
            "atomic_energies",
            torch.tensor(atomic_energies, dtype=torch.get_default_dtype()),
        )  # [n_elements, ]

    def forward(
        self, x: torch.Tensor  # one-hot of elements [..., n_elements]
    ) -> torch.Tensor:  # [..., ]
        return torch.matmul(x, self.atomic_energies)

    def __repr__(self):
        formatted_energies = ", ".join([f"{x:.4f}" for x in self.atomic_energies])
        return f"{self.__class__.__name__}(energies=[{formatted_energies}])"


@compile_mode("script")
class RadialEmbeddingBlock(torch.nn.Module):
    def __init__(self, r_max: float, num_bessel: int, num_polynomial_cutoff: int):
        super().__init__()
        self.bessel_fn = BesselBasis(r_max=r_max, num_basis=num_bessel)
        self.cutoff_fn = PolynomialCutoff(r_max=r_max, p=num_polynomial_cutoff)
        self.out_dim = num_bessel

    def forward(
        self,
        edge_lengths: torch.Tensor,  # [n_edges, 1]
    ):
        bessel = self.bessel_fn(edge_lengths)  # [n_edges, n_basis]
        cutoff = self.cutoff_fn(edge_lengths)  # [n_edges, 1]
        return bessel * cutoff  # [n_edges, n_basis]


@compile_mode("script")
class EquivariantProductBasisBlock(torch.nn.Module):
    def __init__(
        self,
        node_feats_irreps: o3.Irreps,
        target_irreps: o3.Irreps,
        correlation: int,
        use_sc: bool = True,
        num_elements: Optional[int] = None,
    ) -> None:
        super().__init__()

        self.use_sc = use_sc
        self.symmetric_contractions = SymmetricContraction(
            irreps_in=node_feats_irreps,
            irreps_out=target_irreps,
            correlation=correlation,
            num_elements=num_elements,
        )
        # Update linear
        self.linear = o3.Linear(
            target_irreps,
            target_irreps,
            internal_weights=True,
            shared_weights=True,
        )

    def forward(
        self,
        node_feats: torch.Tensor,
        sc: Optional[torch.Tensor],
        node_attrs: torch.Tensor,
    ) -> torch.Tensor:
        node_feats = self.symmetric_contractions(node_feats, node_attrs)
        if self.use_sc and sc is not None:
            return self.linear(node_feats) + sc

        return self.linear(node_feats)


@compile_mode("script")
class InteractionBlock(torch.nn.Module):
    def __init__(
        self,
        node_attrs_irreps: o3.Irreps,
        node_feats_irreps: o3.Irreps,
        edge_attrs_irreps: o3.Irreps,
        edge_feats_irreps: o3.Irreps,
        target_irreps: o3.Irreps,
        hidden_irreps: o3.Irreps,
        avg_num_neighbors: float,
        radial_MLP: Optional[List[int]] = None,
    ) -> None:
        super().__init__()
        self.node_attrs_irreps = node_attrs_irreps
        self.node_feats_irreps = node_feats_irreps
        self.edge_attrs_irreps = edge_attrs_irreps
        self.edge_feats_irreps = edge_feats_irreps
        self.target_irreps = target_irreps
        self.hidden_irreps = hidden_irreps
        self.avg_num_neighbors = avg_num_neighbors
        if radial_MLP is None:
            radial_MLP = [64, 64, 64]
        self.radial_MLP = radial_MLP

        self._setup()

    @abstractmethod
    def _setup(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def forward(
        self,
        node_attrs: torch.Tensor,
        node_feats: torch.Tensor,
        edge_attrs: torch.Tensor,
        edge_feats: torch.Tensor,
        edge_index: torch.Tensor,
    ) -> torch.Tensor:
        raise NotImplementedError


nonlinearities = {1: torch.nn.functional.silu, -1: torch.tanh}


@compile_mode("script")
class TensorProductWeightsBlock(torch.nn.Module):
    def __init__(self, num_elements: int, num_edge_feats: int, num_feats_out: int):
        super().__init__()

        weights = torch.empty(
            (num_elements, num_edge_feats, num_feats_out),
            dtype=torch.get_default_dtype(),
        )
        torch.nn.init.xavier_uniform_(weights)
        self.weights = torch.nn.Parameter(weights)

    def forward(
        self,
        sender_or_receiver_node_attrs: torch.Tensor,  # assumes that the node attributes are one-hot encoded
        edge_feats: torch.Tensor,
    ):
        return torch.einsum(
            "be, ba, aek -> bk", edge_feats, sender_or_receiver_node_attrs, self.weights
        )

    def __repr__(self):
        return (
            f'{self.__class__.__name__}(shape=({", ".join(str(s) for s in self.weights.shape)}), '
            f"weights={np.prod(self.weights.shape)})"
        )


@compile_mode("script")
class ResidualElementDependentInteractionBlock(InteractionBlock):
    def _setup(self) -> None:
        self.linear_up = o3.Linear(
            self.node_feats_irreps,
            self.node_feats_irreps,
            internal_weights=True,
            shared_weights=True,
        )
        # TensorProduct
        irreps_mid, instructions = tp_out_irreps_with_instructions(
            self.node_feats_irreps, self.edge_attrs_irreps, self.target_irreps
        )
        self.conv_tp = o3.TensorProduct(
            self.node_feats_irreps,
            self.edge_attrs_irreps,
            irreps_mid,
            instructions=instructions,
            shared_weights=False,
            internal_weights=False,
        )
        self.conv_tp_weights = TensorProductWeightsBlock(
            num_elements=self.node_attrs_irreps.num_irreps,
            num_edge_feats=self.edge_feats_irreps.num_irreps,
            num_feats_out=self.conv_tp.weight_numel,
        )

        # Linear
        irreps_mid = irreps_mid.simplify()
        self.irreps_out = linear_out_irreps(irreps_mid, self.target_irreps)
        self.irreps_out = self.irreps_out.simplify()
        self.linear = o3.Linear(
            irreps_mid, self.irreps_out, internal_weights=True, shared_weights=True
        )

        # Selector TensorProduct
        self.skip_tp = o3.FullyConnectedTensorProduct(
            self.node_feats_irreps, self.node_attrs_irreps, self.irreps_out
        )

    def forward(
        self,
        node_attrs: torch.Tensor,
        node_feats: torch.Tensor,
        edge_attrs: torch.Tensor,
        edge_feats: torch.Tensor,
        edge_index: torch.Tensor,
    ) -> torch.Tensor:
        sender = edge_index[0]
        receiver = edge_index[1]
        num_nodes = node_feats.shape[0]
        sc = self.skip_tp(node_feats, node_attrs)
        node_feats = self.linear_up(node_feats)
        tp_weights = self.conv_tp_weights(node_attrs[sender], edge_feats)
        mji = self.conv_tp(
            node_feats[sender], edge_attrs, tp_weights
        )  # [n_edges, irreps]
        message = scatter_sum(
            src=mji, index=receiver, dim=0, dim_size=num_nodes
        )  # [n_nodes, irreps]
        message = self.linear(message) / self.avg_num_neighbors
        return message + sc  # [n_nodes, irreps]


@compile_mode("script")
class AgnosticNonlinearInteractionBlock(InteractionBlock):
    def _setup(self) -> None:
        self.linear_up = o3.Linear(
            self.node_feats_irreps,
            self.node_feats_irreps,
            internal_weights=True,
            shared_weights=True,
        )
        # TensorProduct
        irreps_mid, instructions = tp_out_irreps_with_instructions(
            self.node_feats_irreps, self.edge_attrs_irreps, self.target_irreps
        )
        self.conv_tp = o3.TensorProduct(
            self.node_feats_irreps,
            self.edge_attrs_irreps,
            irreps_mid,
            instructions=instructions,
            shared_weights=False,
            internal_weights=False,
        )

        # Convolution weights
        input_dim = self.edge_feats_irreps.num_irreps
        self.conv_tp_weights = nn.FullyConnectedNet(
            [input_dim] + self.radial_MLP + [self.conv_tp.weight_numel],
            torch.nn.functional.silu,
        )

        # Linear
        irreps_mid = irreps_mid.simplify()
        self.irreps_out = linear_out_irreps(irreps_mid, self.target_irreps)
        self.irreps_out = self.irreps_out.simplify()
        self.linear = o3.Linear(
            irreps_mid, self.irreps_out, internal_weights=True, shared_weights=True
        )

        # Selector TensorProduct
        self.skip_tp = o3.FullyConnectedTensorProduct(
            self.irreps_out, self.node_attrs_irreps, self.irreps_out
        )

    def forward(
        self,
        node_attrs: torch.Tensor,
        node_feats: torch.Tensor,
        edge_attrs: torch.Tensor,
        edge_feats: torch.Tensor,
        edge_index: torch.Tensor,
    ) -> torch.Tensor:
        sender = edge_index[0]
        receiver = edge_index[1]
        num_nodes = node_feats.shape[0]
        tp_weights = self.conv_tp_weights(edge_feats)
        node_feats = self.linear_up(node_feats)
        mji = self.conv_tp(
            node_feats[sender], edge_attrs, tp_weights
        )  # [n_edges, irreps]
        message = scatter_sum(
            src=mji, index=receiver, dim=0, dim_size=num_nodes
        )  # [n_nodes, irreps]
        message = self.linear(message) / self.avg_num_neighbors
        message = self.skip_tp(message, node_attrs)
        return message  # [n_nodes, irreps]


@compile_mode("script")
class AgnosticResidualNonlinearInteractionBlock(InteractionBlock):
    def _setup(self) -> None:
        # First linear
        self.linear_up = o3.Linear(
            self.node_feats_irreps,
            self.node_feats_irreps,
            internal_weights=True,
            shared_weights=True,
        )
        # TensorProduct
        irreps_mid, instructions = tp_out_irreps_with_instructions(
            self.node_feats_irreps, self.edge_attrs_irreps, self.target_irreps
        )
        self.conv_tp = o3.TensorProduct(
            self.node_feats_irreps,
            self.edge_attrs_irreps,
            irreps_mid,
            instructions=instructions,
            shared_weights=False,
            internal_weights=False,
        )

        # Convolution weights
        input_dim = self.edge_feats_irreps.num_irreps
        self.conv_tp_weights = nn.FullyConnectedNet(
            [input_dim] + self.radial_MLP + [self.conv_tp.weight_numel],
            torch.nn.functional.silu,
        )

        # Linear
        irreps_mid = irreps_mid.simplify()
        self.irreps_out = linear_out_irreps(irreps_mid, self.target_irreps)
        self.irreps_out = self.irreps_out.simplify()
        self.linear = o3.Linear(
            irreps_mid, self.irreps_out, internal_weights=True, shared_weights=True
        )

        # Selector TensorProduct
        self.skip_tp = o3.FullyConnectedTensorProduct(
            self.node_feats_irreps, self.node_attrs_irreps, self.irreps_out
        )

    def forward(
        self,
        node_attrs: torch.Tensor,
        node_feats: torch.Tensor,
        edge_attrs: torch.Tensor,
        edge_feats: torch.Tensor,
        edge_index: torch.Tensor,
    ) -> torch.Tensor:
        sender = edge_index[0]
        receiver = edge_index[1]
        num_nodes = node_feats.shape[0]
        sc = self.skip_tp(node_feats, node_attrs)
        node_feats = self.linear_up(node_feats)
        tp_weights = self.conv_tp_weights(edge_feats)
        mji = self.conv_tp(
            node_feats[sender], edge_attrs, tp_weights
        )  # [n_edges, irreps]
        message = scatter_sum(
            src=mji, index=receiver, dim=0, dim_size=num_nodes
        )  # [n_nodes, irreps]
        message = self.linear(message) / self.avg_num_neighbors
        message = message + sc
        return message  # [n_nodes, irreps]


@compile_mode("script")
class RealAgnosticInteractionBlock(InteractionBlock):
    def _setup(self) -> None:
        # First linear
        self.linear_up = o3.Linear(
            self.node_feats_irreps,
            self.node_feats_irreps,
            internal_weights=True,
            shared_weights=True,
        )
        # TensorProduct
        irreps_mid, instructions = tp_out_irreps_with_instructions(
            self.node_feats_irreps,
            self.edge_attrs_irreps,
            self.target_irreps,
        )
        self.conv_tp = o3.TensorProduct(
            self.node_feats_irreps,
            self.edge_attrs_irreps,
            irreps_mid,
            instructions=instructions,
            shared_weights=False,
            internal_weights=False,
        )

        # Convolution weights
        input_dim = self.edge_feats_irreps.num_irreps
        self.conv_tp_weights = nn.FullyConnectedNet(
            [input_dim] + self.radial_MLP + [self.conv_tp.weight_numel],
            torch.nn.functional.silu,
        )

        # Linear
        irreps_mid = irreps_mid.simplify()
        self.irreps_out = self.target_irreps
        self.linear = o3.Linear(
            irreps_mid, self.irreps_out, internal_weights=True, shared_weights=True
        )

        # Selector TensorProduct
        self.skip_tp = o3.FullyConnectedTensorProduct(
            self.irreps_out, self.node_attrs_irreps, self.irreps_out
        )
        self.reshape = reshape_irreps(self.irreps_out)

    def forward(
        self,
        node_attrs: torch.Tensor,
        node_feats: torch.Tensor,
        edge_attrs: torch.Tensor,
        edge_feats: torch.Tensor,
        edge_index: torch.Tensor,
    ) -> Tuple[torch.Tensor, None]:
        sender = edge_index[0]
        receiver = edge_index[1]
        num_nodes = node_feats.shape[0]
        node_feats = self.linear_up(node_feats)
        tp_weights = self.conv_tp_weights(edge_feats)
        mji = self.conv_tp(
            node_feats[sender], edge_attrs, tp_weights
        )  # [n_edges, irreps]
        message = scatter_sum(
            src=mji, index=receiver, dim=0, dim_size=num_nodes
        )  # [n_nodes, irreps]
        message = self.linear(message) / self.avg_num_neighbors
        message = self.skip_tp(message, node_attrs)
        return (
            self.reshape(message),
            None,
        )  # [n_nodes, channels, (lmax + 1)**2]


@compile_mode("script")
class RealAgnosticResidualInteractionBlock(InteractionBlock):
    def _setup(self) -> None:
        # First linear
        self.linear_up = o3.Linear(
            self.node_feats_irreps,
            self.node_feats_irreps,
            internal_weights=True,
            shared_weights=True,
        )
        # TensorProduct
        irreps_mid, instructions = tp_out_irreps_with_instructions(
            self.node_feats_irreps,
            self.edge_attrs_irreps,
            self.target_irreps,
        )
        self.conv_tp = o3.TensorProduct(
            self.node_feats_irreps,
            self.edge_attrs_irreps,
            irreps_mid,
            instructions=instructions,
            shared_weights=False,
            internal_weights=False,
        )

        # Convolution weights
        input_dim = self.edge_feats_irreps.num_irreps
        self.conv_tp_weights = nn.FullyConnectedNet(
            [input_dim] + self.radial_MLP + [self.conv_tp.weight_numel],
            torch.nn.functional.silu,
        )

        # Linear
        irreps_mid = irreps_mid.simplify()
        self.irreps_out = self.target_irreps
        self.linear = o3.Linear(
            irreps_mid, self.irreps_out, internal_weights=True, shared_weights=True
        )

        # Selector TensorProduct
        self.skip_tp = o3.FullyConnectedTensorProduct(
            self.node_feats_irreps, self.node_attrs_irreps, self.hidden_irreps
        )
        self.reshape = reshape_irreps(self.irreps_out)

    def forward(
        self,
        node_attrs: torch.Tensor,
        node_feats: torch.Tensor,
        edge_attrs: torch.Tensor,
        edge_feats: torch.Tensor,
        edge_index: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        sender = edge_index[0]
        receiver = edge_index[1]
        num_nodes = node_feats.shape[0]
        sc = self.skip_tp(node_feats, node_attrs)
        node_feats = self.linear_up(node_feats)
        tp_weights = self.conv_tp_weights(edge_feats)
        mji = self.conv_tp(
            node_feats[sender], edge_attrs, tp_weights
        )  # [n_edges, irreps]
        message = scatter_sum(
            src=mji, index=receiver, dim=0, dim_size=num_nodes
        )  # [n_nodes, irreps]
        message = self.linear(message) / self.avg_num_neighbors
        return (
            self.reshape(message),
            sc,
        )  # [n_nodes, channels, (lmax + 1)**2]


class MatrixFunctionBlock(torch.nn.Module):
    def __init__(
        self,
        node_feats_irreps,
        num_features,
        num_basis,
        num_poles,
        avg_num_neighbors,
        g_scaling="1",
        diagonal="laplacian",
        learnable_resolvent = False,
        skip_connection = False
    ):
        super().__init__()
        # First linear
        self.diagonal = diagonal
        self.node_feats_irreps = node_feats_irreps
        self.avg_num_neighbors = avg_num_neighbors
        self.num_poles = num_poles
        self.learnable_resolvent = learnable_resolvent
        self.skip_connection     = skip_connection
        irreps_scalar = o3.Irreps(
            [(self.node_feats_irreps.count(o3.Irrep(0, 1)), (0, 1))]
        )
        self.linear_scalar = o3.Linear(
            self.node_feats_irreps,
            irreps_scalar,
            internal_weights=True,
            shared_weights=True,
            biases=True,  # TODO: check
        )
        
        # Edge features
        self.edge_feats_mlp = nn.FullyConnectedNet(
            [num_basis] + 3 * [64] + [irreps_scalar.num_irreps],
            torch.nn.functional.silu,
        )
        self.matrix_mlp = nn.FullyConnectedNet(
            [irreps_scalar.num_irreps] + [64] + [64] + [num_features],
            torch.nn.functional.silu,
        )
        if diagonal == "learnable":
            self.diag_matrix_mlp =  nn.FullyConnectedNet(
                [irreps_scalar.num_irreps] + [64] + [64] + [num_features],
                torch.nn.functional.silu,
            )
        if learnable_resolvent:
            z_k_real = (
                torch.randn(1, num_features * num_poles, 1, dtype=torch.get_default_dtype())
                * 1
            )  # TODO: for each feature, create several poles, think about initialization
            z_k_complex = (
                torch.randn(1, num_features * num_poles, 1, dtype=torch.get_default_dtype())
                * 1
            )  # TODO: HACK need to think about loss function a bit + initialization
            
            self.z_k_real = torch.nn.Parameter(z_k_real, requires_grad=True)
            self.z_k_complex = torch.nn.Parameter(z_k_complex, requires_grad=True)
        if skip_connection:
            self.normalize_out = nn.BatchNorm(node_feats_irreps,momentum = 0.003)
        self.matrix_norm = EigenvalueNorm(num_features * num_poles)
        self.normalize_real = SwitchNorm1d(num_features * num_poles)
        self.normalize_complex = SwitchNorm1d(num_features * num_poles)
        self.linear_out = o3.Linear(
            o3.Irreps(f"{2*num_features * num_poles}x0e"),  # 2* for real and imaginary
            self.node_feats_irreps,
            internal_weights=True,
            shared_weights=True,
        )
        self.g_scaling = eval(f"lambda x: {g_scaling}")

    def forward(
        self,
        node_feats: torch.Tensor,
        edge_feats: torch.Tensor,
        matrix_feats: torch.Tensor,
        edge_index: torch.Tensor,
        batch: torch.Tensor,
        ptr: torch.Tensor,
    ) -> Tuple[torch.Tensor]:
        sender, receiver = edge_index
        mask = get_mask(ptr[1:] - ptr[:-1])
        mask_matrix = mask.reshape(
            (ptr[1:] - ptr[:-1]).shape[0],
            (ptr[1:] - ptr[:-1]).max())
        mask_matrix = mask_matrix.to(node_feats.device)
        node_feats_org = node_feats
        node_feats = self.linear_scalar(node_feats)
        edge_feats_weights = self.edge_feats_mlp(edge_feats)
        symmetric_node_feats = torch.cat(
            [node_feats[sender] * node_feats[receiver] * edge_feats_weights], dim=1
        )  # TODO: add cutoff function
        H = self.matrix_mlp(
            symmetric_node_feats
        )  # [n_edges, n_features]


        H_dense = to_dense_adj(edge_index=edge_index, batch=batch, edge_attr=H).permute(0, 3, 1, 2)  # [n_graphs, n_features, n_nodes, n_nodes]
        H_dense = H_dense.repeat(1, self.num_poles, 1, 1)

        # Set diagonal elemetns of matrix
        if self.diagonal == "learnable":
            diag_H = self.diag_matrix_mlp(node_feats)
            diag_H = to_dense_batch(diag_H, batch)[0].permute(0, 2, 1)
            diag_H = diag_H.repeat(1, self.num_poles, 1)
            H_dense = H_dense + torch.diag_embed(diag_H)
        elif self.diagonal == "laplacian":
            # Create Laplacian from weighted adjacency matrix
            degree = torch.sum(torch.abs(H_dense), axis=-1)
            H_dense = torch.diag_embed(degree) - H_dense
        elif self.diagonal == "normalised_laplacian":
            raise NotImplementedError()  # TODO: add normalised laplacian
            # Create Laplacian from weighted adjacency matrix
            degree = torch.sum(torch.abs(H_dense), axis=-1)
            degree_inv = (degree + 1e-9) ** (-0.5)[..., None]
            H_dense = (H_dense * degree_inv).T * degree_inv

        # Adding small number to diagonal to avoid padded regions to have zeros
        H_dense = (
            H_dense
            + torch.diag_embed(torch.ones(H_dense.shape[:-1]), dim1=-2, dim2=-1).to(
                H_dense.device
            )
            * 1e-9
        )

        # Add matrix features from previous layer
        
        if matrix_feats is not None:
            H_dense = H_dense + matrix_feats
        if self.learnable_resolvent:
            z_k = torch.view_as_complex(
                torch.stack([self.z_k_real, torch.exp(self.z_k_complex)], dim=-1)
            )
            z_k = z_k.expand(H_dense.shape[0], H_dense.shape[1], H_dense.shape[-1])
        else:
            z_k = torch.view_as_complex(
                torch.stack([torch.zeros_like(H_dense[...,0]), torch.ones_like(H_dense[...,0])], dim=-1) 
            ) 
        D_z = torch.diag_embed(z_k)
        H_dense = self.matrix_norm(H_dense,mask_matrix)
        R_dense = D_z - H_dense


        LUP = torch.linalg.lu_factor_ex(R_dense)
        LU, P = LUP.LU, LUP.pivots
        self.identity = (
            torch.eye(R_dense.shape[-1], dtype=D_z.dtype, device=D_z.device)
            .unsqueeze(0)
            .repeat(R_dense.shape[0], R_dense.shape[1], 1, 1)
        )
        features = torch.linalg.lu_solve(LU, P, self.identity) 
        # [n_graphs, n_features, n_nodes, n_nodes]
        features = features.diagonal(dim1=-2, dim2=-1) * self.g_scaling(z_k)# * z_k.imag
        
        node_features_real = (
            features.real  # [n_graphs, n_features, n_nodes]
            .permute(0, 2, 1)  # [n_graphs, n_nodes, n_features]
            .reshape(
                features.real.shape[0] * features.real.shape[2], features.real.shape[1]
            )
            #  [n_graphs * n_nodes, n_features]
        )
        node_features_imag = (
            features.imag  # [n_graphs, n_features, n_nodes]
            .permute(0, 2, 1)  # [n_graphs, n_nodes, n_features]
            .reshape(
                features.imag.shape[0] * features.imag.shape[2], features.imag.shape[1]
            )
            #  [n_graphs * n_nodes, n_features]
        )

        # Normalise node features (imaginary/real separately)
        node_features_imag = (
            self.normalize_complex(node_features_imag[mask, :]) / self.avg_num_neighbors
        )
        node_features_real = (
            self.normalize_real(node_features_real[mask, :]) / self.avg_num_neighbors
        )

        node_features = torch.cat([node_features_real, node_features_imag], dim=1)
        #  [n_graphs * n_nodes, 2*n_features]
        out = self.linear_out(node_features)
        if self.skip_connection:
            out = self.normalize_out(out + node_feats_org)
        # breakpoint()
        if matrix_feats is None:
            return out, None  # [n_graphs * n_nodes, irreps]
        else:
            return (
                out,
                features.real,
            )  # [n_graphs * n_nodes, irreps]


class SwitchNorm1d(torch.nn.Module):
    def __init__(
        self, num_features, eps=1e-5, momentum=0.997, using_moving_average=True
    ):
        super(SwitchNorm1d, self).__init__()
        self.eps = eps
        self.momentum = momentum
        self.using_moving_average = using_moving_average
        self.weight = torch.nn.Parameter(torch.ones(1, num_features))
        self.bias = torch.nn.Parameter(torch.zeros(1, num_features))
        self.mean_weight = torch.nn.Parameter(torch.ones(2))
        self.var_weight = torch.nn.Parameter(torch.ones(2))
        self.register_buffer("running_mean", torch.zeros(1, num_features))
        self.register_buffer("running_var", torch.zeros(1, num_features))
        self.reset_parameters()

    def reset_parameters(self):
        self.running_mean.zero_()
        self.running_var.zero_()
        self.weight.data.fill_(1)
        self.bias.data.zero_()

    def _check_input_dim(self, input):
        if input.dim() != 2:
            raise ValueError("expected 2D input (got {}D input)".format(input.dim()))

    def forward(self, x):
        self._check_input_dim(x)
        mean_ln = x.mean(1, keepdim=True)
        var_ln = x.var(1, keepdim=True)

        if self.training:
            mean_bn = x.mean(0, keepdim=True)
            var_bn = x.var(0, keepdim=True)
            if self.using_moving_average:
                self.running_mean.mul_(self.momentum)
                self.running_mean.add_((1 - self.momentum) * mean_bn.data)
                self.running_var.mul_(self.momentum)
                self.running_var.add_((1 - self.momentum) * var_bn.data)
            else:
                self.running_mean.add_(mean_bn.data)
                self.running_var.add_(mean_bn.data**2 + var_bn.data)
        else:
            mean_bn = torch.autograd.Variable(self.running_mean)
            var_bn = torch.autograd.Variable(self.running_var)

        softmax = torch.nn.Softmax(0)
        mean_weight = softmax(self.mean_weight)
        var_weight = softmax(self.var_weight)

        mean = mean_weight[0] * mean_ln + mean_weight[1] * mean_bn
        var = var_weight[0] * var_ln + var_weight[1] * var_bn

        x = (x - mean) / (var + self.eps).sqrt()
        return x * self.weight + self.bias

# Eigenvalue normalization layer
class EigenvalueNorm(torch.nn.Module):
    def __init__(self,num_features, eps=1e-5, momentum=0.997, using_moving_average=True):
        super(EigenvalueNorm, self).__init__()
        self.eps = eps  # Small constant for numerical stability
        self.momentum = momentum  # Momentum for moving average
        self.using_moving_average = using_moving_average  # Whether to use moving average or not
        self.num_features = num_features  # Number of feature channels
        self.weight = torch.nn.Parameter(torch.ones(1, num_features,1,1))  # Learnable weight for scaling
        self.bias = torch.nn.Parameter(torch.zeros(1, num_features,1))  # Learnable bias for shifting
        self.register_buffer("running_mean", torch.zeros(num_features))  # Running mean of eigenvalues
        self.register_buffer("running_var", torch.ones(num_features))  # Running variance of eigenvalues
        self.reset_parameters()

    # Reset parameters (running mean and variance)
    def reset_parameters(self):
        self.running_mean.zero_()
        self.running_var.fill_(1)
        self.bias.data.zero_()
        self.weight.data.fill_(1)

    # Check the dimensions of the input tensor
    def _check_input_dim(self, input):
        if input.dim() != 4:
            raise ValueError("expected input 4 dimensions (got {}D input)".format(input.dim()))
        if input.shape[1] != self.num_features:
            raise ValueError(f"expected num_features to be {self.num_features}, got {input.shape[1]}")

    # Compute the mean and variance using the trace method
    def _mean_and_variance_trace(self,matrix,mask):
        n = torch.einsum('bfii->bf', mask)
        n2 = torch.clamp(n - 1,1)# Clamp at n - 1 = 1 to avoid division by zero
        matrix_squared = torch.linalg.matrix_power(matrix,2)*mask
        trace = torch.einsum('bfii->bf', matrix*mask)
        trace_square = torch.einsum('bfii->bf', matrix_squared*mask)
        mean = trace / n
        variance = trace_square / n2 - trace ** 2/ (n*n2)
        return mean.mean(0), variance.mean(0)

    # Reformat the mask tensor to match the input tensor shape
    def _reformat_mask(self,mask):
        mask = mask[:,None,:,None] * mask[:,None,None,:]
        mask = mask.expand(-1,self.num_features,-1,-1)
        return mask

    # Forward function for the normalization layer
    def forward(self, x,mask = None):
        # x: [batch_size, num_features, N, N]
        # mask: [batch_size, N]
        self._check_input_dim(x)  # Check input dimensions
        if mask is None:
            mask = torch.ones(x.shape)  # Create a mask with all ones if no mask is provided
        else:
            mask = self._reformat_mask(mask)  # Reformat the mask tensor to match the input tensor shape

        if self.training:
            mean, variance = self._mean_and_variance_trace(x, mask)  # Compute mean and variance using the trace method

            if self.using_moving_average:
                self.running_mean.mul_(self.momentum)  # Update running mean with momentum
                self.running_mean.add_((1 - self.momentum) * mean.data)  # Update running mean with new mean
                self.running_var.mul_(self.momentum)  # Update running variance with momentum
                self.running_var.add_((1 - self.momentum) * variance.data)  # Update running variance with new variance
            else:
                self.running_mean.add_(mean.data)  # Update running mean without momentum
                self.running_var.add_(variance.data)  # Update running variance without momentum

        m = torch.autograd.Variable(self.running_mean)  # Running mean variable
        v = torch.autograd.Variable(self.running_var)  # Running variance variable

        m_t = torch.diag_embed(m[None, :, None].expand_as(x[..., 0]), offset=0, dim1=-2, dim2=-1)  # Diagonal matrix of the running mean
        x_centered = (x - m_t) * mask  # Subtract the running mean from the input tensor
        x_normalized = x_centered / (v[None, :, None, None].expand_as(x) + self.eps).sqrt()  # Normalize the centered tensor

        return x_normalized * self.weight + torch.diag_embed(self.bias.expand_as(x[..., 0]), offset=0, dim1=-2, dim2=-1)  # Return the normalized tensor scaled by the learned weight and shifted by the learned bias

@compile_mode("script")
class ScaleShiftBlock(torch.nn.Module):
    def __init__(self, scale: float, shift: float):
        super().__init__()
        self.register_buffer(
            "scale", torch.tensor(scale, dtype=torch.get_default_dtype())
        )
        self.register_buffer(
            "shift", torch.tensor(shift, dtype=torch.get_default_dtype())
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.scale * x + self.shift

    def __repr__(self):
        return (
            f"{self.__class__.__name__}(scale={self.scale:.6f}, shift={self.shift:.6f})"
        )
