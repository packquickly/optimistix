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

from typing import Any, cast, Optional, Union

import equinox as eqx
import jax.tree_util as jtu
from jaxtyping import PyTree

from ._adjoint import AbstractAdjoint, ImplicitAdjoint
from ._custom_types import Aux, Fn, MaybeAuxFn, Out, SolverState, Y
from ._iterate import AbstractIterativeSolver, iterative_solve
from ._least_squares import AbstractLeastSquaresSolver, least_squares
from ._minimise import AbstractMinimiser
from ._misc import inexact_asarray, max_norm, NoneAux
from ._solution import RESULTS, Solution


class AbstractRootFinder(AbstractIterativeSolver[Y, Out, Aux, SolverState]):
    """Abstract base class for all root finders."""


def _rewrite_fn(root, _, inputs):
    root_fn, _, _, args, *_ = inputs
    del inputs
    f_val, _ = root_fn(root, args)
    return f_val


@eqx.filter_jit
def root_find(
    fn: MaybeAuxFn[Y, Out, Aux],
    # no type parameters, see https://github.com/microsoft/pyright/discussions/5599
    solver: Union[AbstractRootFinder, AbstractLeastSquaresSolver, AbstractMinimiser],
    y0: Y,
    args: PyTree = None,
    options: Optional[dict[str, Any]] = None,
    *,
    has_aux: bool = False,
    max_steps: Optional[int] = 256,
    adjoint: AbstractAdjoint = ImplicitAdjoint(),
    throw: bool = True,
    tags: frozenset[object] = frozenset(),
) -> Solution[Y, Aux]:
    """Solve a root-finding problem.

    Given a nonlinear function `fn(y, args)` which returns a pytree of arrays,
    this returns the value `z` such that `fn(z, args) = 0`.

    **Arguments:**

    - `fn`: The function to find the roots of. This should take two arguments:
        `fn(y, args)` and return a pytree of arrays not necessarily of the same shape
        as the input `y`.
    - `solver`: The root-finder to use. This should be an
        [`optimistix.AbstractRootFinder`][],
        [`optimistix.AbstractLeastSquaresSolver`][], or
        [`optimistix.AbstractMinimiser`][]. If it is a least-squares solver or a
        minimiser, then the value `sum(fn(y, args)^2)` is minimised.
    - `y0`: An initial guess for what `y` may be.
    - `args`: Passed as the `args` of `fn(y, args)`.
    - `options`: Individual solvers may accept additional runtime arguments.
        See each individual solver's documentation for more details.
    - `has_aux`: If `True`, then `fn` may return a pair, where the first element is its
        function value, and the second is just auxiliary data. Keyword only argument.
    - `max_steps`: The maximum number of steps the solver can take. Keyword only
        argument.
    - `adjoint`: The adjoint method used to compute gradients through the fixed-point
        solve. Keyword only argument.
    - `throw`: How to report any failures. (E.g. an iterative solver running out of
        steps, or encountering divergent iterates.) If `True` then a failure will raise
        an error. If `False` then the returned solution object will have a `result`
        field indicating whether any failures occured. (See [`optimistix.Solution`][].)
        Keyword only argument.
    - `tags`: Lineax [tags](https://docs.kidger.site/lineax/api/tags/) describing the
        any structure of the Jacobian of `fn` with respect to `y`. Used with some
        solvers (e.g. [`optimistix.Newton`][]), and with some adjoint methods (e.g.
        [`optimistix.ImplicitAdjoint`][]) to improve the efficiency of linear solves.
        Keyword only argument.

    **Returns:**

    An [`optimistix.Solution`][] object.
    """

    if not has_aux:
        fn = NoneAux(fn)  # pyright: ignore

    if isinstance(solver, (AbstractMinimiser, AbstractLeastSquaresSolver)):
        del tags
        # `fn` is unchanged, as `least_squares` expects the residuals.
        sol = least_squares(
            fn,
            solver,
            y0,
            args,
            options,
            has_aux=True,
            max_steps=max_steps,
            adjoint=adjoint,
            throw=False,
        )
        # This is an ugly heuristic. I'd welcome any thoughts on how to improve it.
        # Consider trying to find a root of x->1+x^2 with a minimiser. It obviously
        # won't find a root, but it will find a local minimum and declare success.
        # So we need to check that we've actually got a root and raise an error if not.
        #
        # What should the tolerance on that be? At first you might think we could do
        # `solver.norm(f_val) < solver.aval`, but this doesn't work: the original solver
        # may terminate before this condition is satisfied. In particular this happens
        # because minimisers often stop once a Cauchy condition is satisfied, i.e. two
        # adjacent iterates are close. That offers no guarantees that the above is also
        # satisfied (an indeed in practice it frequently is not).
        #
        # So, this is a best-effort attempt: max norm with tolerance 0.1, hardcoded.
        f_val = fn(sol.value, args)
        did_not_find_root = max_norm(f_val) > 0.1
        result = RESULTS.where(
            did_not_find_root & (sol.result == RESULTS.successful),
            RESULTS.nonlinear_root_conversion_failed,
            sol.result,
        )
        sol = eqx.tree_at(lambda s: s.result, sol, result)
        if throw:
            sol = sol.result.error_if(sol, sol.result != RESULTS.successful)
        return sol
    else:
        y0 = jtu.tree_map(inexact_asarray, y0)
        fn = eqx.filter_closure_convert(fn, y0, args)  # pyright: ignore
        fn = cast(Fn[Y, Out, Aux], fn)
        f_struct, aux_struct = fn.out_struct
        if options is None:
            options = {}
        return iterative_solve(
            fn,
            solver,
            y0,
            args,
            options,
            max_steps=max_steps,
            adjoint=adjoint,
            throw=throw,
            tags=tags,
            f_struct=f_struct,
            aux_struct=aux_struct,
            rewrite_fn=_rewrite_fn,
        )
