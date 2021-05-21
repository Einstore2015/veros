from petsc4py import PETSc
import numpy as onp

from veros import logger, runtime_settings as rs, runtime_state as rst
from veros.core import utilities
from veros.core.streamfunction.solvers.base import LinearSolver
from veros.core.operators import numpy as npx, update, update_add, at


class PETScSolver(LinearSolver):
    def __init__(self, state):
        settings = state.settings

        if settings.enable_cyclic_x:
            boundary_type = ("periodic", "ghosted")
        else:
            boundary_type = ("ghosted", "ghosted")

        self._da = PETSc.DMDA().create(
            [settings.nx, settings.ny],
            stencil_width=1,
            stencil_type="star",
            comm=rs.mpi_comm,
            proc_sizes=rs.num_proc,
            boundary_type=boundary_type,
            ownership_ranges=[
                (settings.nx // rs.num_proc[0],) * rs.num_proc[0],
                (settings.ny // rs.num_proc[1],) * rs.num_proc[1],
            ],
        )

        self._matrix, self._boundary_fac = self._assemble_poisson_matrix(state)

        petsc_options = PETSc.Options()

        # setup krylov method
        self._ksp = PETSc.KSP()
        self._ksp.create(rs.mpi_comm)
        self._ksp.setOperators(self._matrix)
        self._ksp.setType("gmres")
        self._ksp.setTolerances(atol=0, rtol=settings.congr_epsilon, max_it=settings.congr_max_iterations)

        # preconditioner
        self._ksp.getPC().setType("hypre")
        petsc_options["pc_hypre_type"] = "boomeramg"
        petsc_options["pc_hypre_boomeramg_relax_type_all"] = "SOR/Jacobi"
        self._ksp.getPC().setFromOptions()

        self._rhs_petsc = self._da.createGlobalVec()
        self._sol_petsc = self._da.createGlobalVec()

    def _petsc_solver(self, state, rhs, x0):
        settings = state.settings

        # add dirichlet BC to rhs
        if not settings.enable_cyclic_x:
            if rst.proc_idx[0] == rs.num_proc[0] - 1:
                rhs = update_add(rhs, at[-3, 2:-2], -rhs[-2, 2:-2] * self._boundary_fac["east"])

            if rst.proc_idx[0] == 0:
                rhs = update_add(rhs, at[2, 2:-2], -rhs[1, 2:-2] * self._boundary_fac["west"])

        if rst.proc_idx[1] == rs.num_proc[1] - 1:
            rhs = update_add(rhs, at[2:-2, -3], -rhs[2:-2, -2] * self._boundary_fac["north"])

        if rst.proc_idx[1] == 0:
            rhs = update_add(rhs, at[2:-2, 2], -rhs[2:-2, 1] * self._boundary_fac["south"])

        self._da.getVecArray(self._rhs_petsc)[...] = rhs[2:-2, 2:-2]
        self._da.getVecArray(self._sol_petsc)[...] = x0[2:-2, 2:-2]

        self._ksp.solve(self._rhs_petsc, self._sol_petsc)

        info = self._ksp.getConvergedReason()
        iterations = self._ksp.getIterationNumber()

        if info < 0:
            logger.warning(f"Streamfunction solver did not converge after {iterations} iterations (error code: {info})")

        return npx.array(self._da.getVecArray(self._sol_petsc)[...])

    def solve(self, state, rhs, x0, boundary_val=None):
        """
        Arguments:
            rhs: Right-hand side vector
            x0: Initial guess
            boundary_val: Array containing values to set on boundary elements. Defaults to `x0`.
        """
        vs = state.variables
        settings = state.settings

        if boundary_val is None:
            boundary_val = x0

        x0 = utilities.enforce_boundaries(x0, settings.enable_cyclic_x)

        boundary_mask = npx.all(~vs.boundary_mask, axis=2)
        rhs = npx.where(boundary_mask, rhs, boundary_val)  # set right hand side on boundaries

        linear_solution = self._petsc_solver(vs, rhs, x0)

        return update(rhs, at[2:-2, 2:-2], linear_solution)

    def _assemble_poisson_matrix(self, state):
        """
        Construct a sparse matrix based on the stencil for the 2D Poisson equation.
        """
        vs = state.variables
        settings = state.settings

        matrix = self._da.getMatrix()

        boundary_mask = npx.all(~vs.boundary_mask[2:-2, 2:-2], axis=2)

        # assemble diagonals
        main_diag = (
            -vs.hvr[3:-1, 2:-2]
            / vs.dxu[2:-2, npx.newaxis]
            / vs.dxt[3:-1, npx.newaxis]
            / vs.cosu[npx.newaxis, 2:-2] ** 2
            - vs.hvr[2:-2, 2:-2]
            / vs.dxu[2:-2, npx.newaxis]
            / vs.dxt[2:-2, npx.newaxis]
            / vs.cosu[npx.newaxis, 2:-2] ** 2
            - vs.hur[2:-2, 2:-2]
            / vs.dyu[npx.newaxis, 2:-2]
            / vs.dyt[npx.newaxis, 2:-2]
            * vs.cost[npx.newaxis, 2:-2]
            / vs.cosu[npx.newaxis, 2:-2]
            - vs.hur[2:-2, 3:-1]
            / vs.dyu[npx.newaxis, 2:-2]
            / vs.dyt[npx.newaxis, 3:-1]
            * vs.cost[npx.newaxis, 3:-1]
            / vs.cosu[npx.newaxis, 2:-2]
        )
        east_diag = (
            vs.hvr[3:-1, 2:-2] / vs.dxu[2:-2, npx.newaxis] / vs.dxt[3:-1, npx.newaxis] / vs.cosu[npx.newaxis, 2:-2] ** 2
        )
        west_diag = (
            vs.hvr[2:-2, 2:-2] / vs.dxu[2:-2, npx.newaxis] / vs.dxt[2:-2, npx.newaxis] / vs.cosu[npx.newaxis, 2:-2] ** 2
        )
        north_diag = (
            vs.hur[2:-2, 3:-1]
            / vs.dyu[npx.newaxis, 2:-2]
            / vs.dyt[npx.newaxis, 3:-1]
            * vs.cost[npx.newaxis, 3:-1]
            / vs.cosu[npx.newaxis, 2:-2]
        )
        south_diag = (
            vs.hur[2:-2, 2:-2]
            / vs.dyu[npx.newaxis, 2:-2]
            / vs.dyt[npx.newaxis, 2:-2]
            * vs.cost[npx.newaxis, 2:-2]
            / vs.cosu[npx.newaxis, 2:-2]
        )

        main_diag *= boundary_mask
        main_diag = npx.where(main_diag == 0.0, 1.0, main_diag)

        # construct sparse matrix
        cf = tuple(
            # copy to NumPy for speed
            onp.asarray(diag)
            for diag in (
                main_diag,
                boundary_mask * east_diag,
                boundary_mask * west_diag,
                boundary_mask * north_diag,
                boundary_mask * south_diag,
            )
        )

        row = PETSc.Mat.Stencil()
        col = PETSc.Mat.Stencil()

        ij_offsets = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)]

        (i0, i1), (j0, j1) = self._da.getRanges()
        for j in range(j0, j1):
            for i in range(i0, i1):
                iloc, jloc = i % (settings.nx // rs.num_proc[0]), j % (settings.ny // rs.num_proc[1])
                row.index = (i, j)

                for diag, offset in zip(cf, ij_offsets):
                    io, jo = (i + offset[0], j + offset[1])
                    col.index = (io, jo)
                    matrix.setValueStencil(row, col, diag[iloc, jloc])

        matrix.assemble()

        boundary_scale = {
            "east": npx.asarray(cf[1][-1, :]),
            "west": npx.asarray(cf[2][0, :]),
            "north": npx.asarray(cf[3][:, -1]),
            "south": npx.asarray(cf[4][:, 0]),
        }

        return matrix, boundary_scale
