# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
from collections.abc import Callable, Sequence
from typing import Any, Generic, Optional, TYPE_CHECKING, Union

import equinox as eqx
import equinox.internal as eqxi
import jax
import jax.core
import jax.numpy as jnp
import jax.tree_util as jtu
from jaxtyping import Array, Bool, PyTree

from ._adjoint import AbstractAdjoint
from ._custom_types import Aux, Fn, Out, SolverState, Y
from ._solution import RESULTS, Solution


if TYPE_CHECKING:
    _Node = Any
else:
    _Node = eqxi.doc_repr(Any, "Node")


def _is_jaxpr(x):
    return isinstance(x, (jax.core.Jaxpr, jax.core.ClosedJaxpr))


def _is_array_or_jaxpr(x):
    return _is_jaxpr(x) or eqx.is_array(x)


class AbstractIterativeSolver(eqx.Module, Generic[SolverState, Y, Out, Aux]):
    """Abstract base class for all iterative solvers."""

    @abc.abstractmethod
    def init(
        self,
        fn: Fn[Y, Out, Aux],
        y: Y,
        args: PyTree,
        options: dict[str, Any],
        f_struct: PyTree[jax.ShapeDtypeStruct],
        aux_struct: PyTree[jax.ShapeDtypeStruct],
        tags: frozenset[object],
    ) -> SolverState:
        """Perform all initial computation needed to initialise the solver state.

        For example, the [`optimistix.Chord`][] method computes the Jacobian `df/dy`
        with respect to the initial guess `y`, and then uses it throughout the
        computation.

        **Arguments:**

        - `fn`: The function to iterate over. This is expected to take two argumetns
            `fn(y, args)` and return a pytree of arrays in the first element, and any
            auxiliary data in the second argument.
        - `y`: The value of `y` at the current (first) iteration.
        - `args`: Passed as the `args` of `fn(y, args)`.
        - `options`: Individual solvers may accept additional runtime arguments.
            See each individual solver's documentation for more details.
        - `f_struct`: A pytree of `jax.ShapeDtypeStruct`s of the same shape as the
            output of `fn`. This is used to initialise any information in the state
            which may rely on the pytree structure, array shapes, or dtype of the
            output of `fn`.
        - `aux_struct`: A pytree of `jax.ShapeDtypeStruct`s of the same shape as the
            auxiliary data returned by `fn`.
        - `tags`: exact meaning depends on whether this is a fixed point, root find,
            least squares, or minimisation problem; see their relevant entry points.

        **Returns:**

        A PyTree representing the initial state of the solver.
        """
        ...

    @abc.abstractmethod
    def step(
        self,
        fn: Fn[Y, Out, Aux],
        y: Y,
        args: PyTree,
        options: dict[str, Any],
        state: SolverState,
        tags: frozenset[object],
    ) -> tuple[Y, SolverState, Aux]:
        """Perform one step of the iterative solve.

        **Arguments:**

        - `fn`: The function to iterate over. This is expected to take two argumetns
            `fn(y, args)` and return a pytree of arrays in the first element, and any
            auxiliary data in the second argument.
        - `y`: The value of `y` at the current (first) iteration.
        - `args`: Passed as the `args` of `fn(y, args)`.
        - `options`: Individual solvers may accept additional runtime arguments.
            See each individual solver's documentation for more details.
        - `state`: A pytree representing the state of a solver. The shape of this
            pytree is solver-dependent.
        - `tags`: exact meaning depends on whether this is a fixed point, root find,
            least squares, or minimisation problem; see their relevant entry points.

        **Returns:**

        A 3-tuple containing the new `y` value in the first element, the next solver
        state in the second element, and the aux output of `fn(y, args)` in the third
        element.
        """
        ...

    @abc.abstractmethod
    def terminate(
        self,
        fn: Fn[Y, Out, Aux],
        y: Y,
        args: PyTree,
        options: dict[str, Any],
        state: SolverState,
        tags: frozenset[object],
    ) -> tuple[Bool[Array, ""], RESULTS]:
        """Determine whether or not to stop the iterative solve.

        **Arguments:**

        - `fn`: The function to iterate over. This is expected to take two argumetns
            `fn(y, args)` and return a pytree of arrays in the first element, and any
            auxiliary data in the second argument.
        - `y`: The value of `y` at the current (first) iteration.
        - `args`: Passed as the `args` of `fn(y, args)`.
        - `options`: Individual solvers may accept additional runtime arguments.
            See each individual solver's documentation for more details.
        - `state`: A pytree representing the state of a solver. The shape of this
            pytree is solver-dependent.
        - `tags`: exact meaning depends on whether this is a fixed point, root find,
            least squares, or minimisation problem; see their relevant entry points.

        **Returns:**

        A 2-tuple containing a bool indicating whether or not to stop iterating in the
        first element, and an [`optimistix.RESULTS`][] object in the second element.
        """
        ...

    @abc.abstractmethod
    def buffers(self, state: SolverState) -> Union[_Node, Sequence[_Node]]:
        """Specifies any write-once buffers in the state.

        See the documentation for `equinox.internal.while_loop(..., buffers=...)`.
        This method will be passed to that argument, which can be used as a performance
        optimisation in some niche use cases.

        Most solvers should just return an empty tuple.
        """
        ...


def _zero(x):
    if isinstance(x, jax.ShapeDtypeStruct):
        return jnp.zeros(x.shape, dtype=x.dtype)
    else:
        return x


def _iterate(inputs, while_loop):
    fn, args, solver, y0, options, max_steps, f_struct, aux_struct, tags = inputs
    del inputs
    static_leaf = lambda x: isinstance(x, eqxi.Static)
    f_struct = jtu.tree_map(lambda x: x.value, f_struct, is_leaf=static_leaf)
    aux_struct = jtu.tree_map(lambda x: x.value, aux_struct, is_leaf=static_leaf)

    if options is None:
        options = {}
    init_aux = jtu.tree_map(_zero, aux_struct)
    init_state = solver.init(fn, y0, args, options, f_struct, aux_struct, tags)
    dynamic_init_state, static_state = eqx.partition(init_state, eqx.is_array)
    init_carry = (
        y0,
        jnp.array(0),
        dynamic_init_state,
        init_aux,
    )

    def cond_fun(carry):
        y, _, dynamic_state, _ = carry
        state = eqx.combine(static_state, dynamic_state)
        terminate, _ = solver.terminate(fn, y, args, options, state, tags)
        return jnp.invert(terminate)

    def body_fun(carry):
        y, num_steps, dynamic_state, _ = carry
        state = eqx.combine(static_state, dynamic_state)
        new_y, new_state, aux = solver.step(fn, y, args, options, state, tags)
        new_dynamic_state, new_static_state = eqx.partition(new_state, eqx.is_array)

        new_static_state_no_jaxpr = eqx.filter(
            new_static_state, _is_jaxpr, inverse=True
        )
        static_state_no_jaxpr = eqx.filter(state, _is_array_or_jaxpr, inverse=True)
        assert eqx.tree_equal(static_state_no_jaxpr, new_static_state_no_jaxpr) is True
        return new_y, num_steps + 1, new_dynamic_state, aux

    def buffers(carry):
        _, _, state, _ = carry
        return solver.buffers(state)

    final_carry = while_loop(
        cond_fun, body_fun, init_carry, max_steps=max_steps, buffers=buffers
    )
    final_y, num_steps, final_state, aux = final_carry
    _final_state = eqx.combine(static_state, final_state)
    terminate, result = solver.terminate(fn, final_y, args, options, _final_state, tags)
    result = RESULTS.where(
        (result == RESULTS.successful) & jnp.invert(terminate),
        RESULTS.nonlinear_max_steps_reached,
        result,
    )
    return final_y, (num_steps, result, final_state, aux)


def iterative_solve(
    fn: Fn[Y, Out, Aux],
    solver: AbstractIterativeSolver,
    y0: PyTree[Array],
    args: PyTree = None,
    options: Optional[dict[str, Any]] = None,
    *,
    rewrite_fn: Callable,
    max_steps: Optional[int],
    adjoint: AbstractAdjoint,
    throw: bool,
    tags: frozenset[object],
    f_struct: PyTree[jax.ShapeDtypeStruct],
    aux_struct: PyTree[jax.ShapeDtypeStruct],
) -> Solution[Y, Aux]:
    """Compute the iterates of an iterative numerical method.

    Given a nonlinear function `fn(y, args)` and an iterative method `solver`,
    this computes the iterates generated by `solver`. This generalises minimisation,
    least-squares, root-finding, and fixed-point iteration to any iterative
    numerical method applied to `fn(y, args)`.

    **Arguments:**

    - `fn`: The function to iterate over. This is expected to take two argumetns
        `fn(y, args)` and return a pytree of arrays in the first element, and any
        auxiliary data in the second argument.
    - `solver`: The solver to use. This should be a subclass of
        [`optimistix.AbstractIterativeSolver`][].
    - `y0`: An initial guess for what `y` may be.
    - `args`: Passed as the `args` of `fn(y, args)`.
    - `options`: Individual solvers may accept additional runtime arguments.
        See each individual solver's documentation for more details.
    - `rewrite_fn`: A function `(root, residual, inputs) -> arbitrary`.
        Keyword only argument. Used in the implicit function theorem.
    - `max_steps`: The maximum number of steps the solver can take. Keyword only
        argument.
    - `adjoint`: The adjoint method used to compute gradients through an iterative
        solve. Keyword only argument.
    - `throw`: How to report any failures. (E.g. an iterative solver running out of
        steps, or encountering divergent iterates.) If `True` then a failure will raise
        an error. If `False` then the returned solution object will have a `result`
        field indicating whether any failures occured. (See [`optimistix.Solution`][].)
        Keyword only argument.
    - `tags`: exact meaning depends on whether this is a fixed point, root find,
        least squares, or minimisation problem; see their relevant entry points.
    - `f_struct`: A pytree of `jax.ShapeDtypeStruct`s of the same shape as the
        output of `fn`. This is used to initialise any information in the state
        which may rely on the pytree structure, array shapes, or dtype of the
        output of `fn`. Keyword only argument.
    - `aux_struct`: A pytree of `jax.ShapeDtypeStruct`s of the same shape as the
        auxiliary data returned by `fn`. Keyword only argument.

    **Returns:**

    An [`optimistix.Solution`][] object.
    """

    f_struct = jtu.tree_map(eqxi.Static, f_struct)
    aux_struct = jtu.tree_map(eqxi.Static, aux_struct)
    inputs = fn, args, solver, y0, options, max_steps, f_struct, aux_struct, tags
    out, (num_steps, result, final_state, aux) = adjoint.apply(
        _iterate, rewrite_fn, inputs, tags
    )
    stats = {"num_steps": num_steps, "max_steps": max_steps}
    sol = Solution(value=out, result=result, state=final_state, aux=aux, stats=stats)
    if throw:
        sol = result.error_if(sol, result != RESULTS.successful)
    return sol
