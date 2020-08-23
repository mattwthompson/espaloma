# separate parameters for every atom, bond, angle, torsion, up to symmetry

from time import time

import matplotlib.pyplot as plt
import numpy as onp
from jax.config import config  # TODO: is there a way to enable this globally?

from scripts.independent_params.plots import plot_residuals

config.update("jax_enable_x64", True)

from espaloma.utils.jax import jax_play_nice_with_scipy
from jax import grad, jit, numpy as np
from scipy.optimize import basinhopping
from simtk import unit

from espaloma.data.alkethoh.ani import get_snapshots_energies_and_forces
from espaloma.data.alkethoh.data import offmols
from espaloma.mm.mm_utils import get_force_targets, MMComponents
from espaloma.mm.mm_utils import get_sim, compute_harmonic_bond_potential
from espaloma.utils.symmetry import get_unique_bonds, canonicalize_order
onp.random.seed(1234)

# TODO: add coupling terms
# TODO: toggle between `compute_harmonic_bond_potential` and alternate parameterization

# TODO: initializer classes
#   initialize at mean values vs. at openff values

def initialize_bonds(offmol, noise_magnitude=1.0):
    # bonds
    pair_inds, bond_inds = get_unique_bonds(offmol)
    n_unique_bonds = len(set(bond_inds))
    n_bond_params = 2 * n_unique_bonds
    bond_params = onp.zeros(n_bond_params)

    sim = get_sim(name)

    # compare pair inds from harmonic_bond_force and autodiff'd one
    harmonic_bond_force = [f for f in sim.system.getForces() if ("HarmonicBond" in f.__class__.__name__)][0]
    omm_pair_inds = []
    omm_bond_params = dict()

    for i in range(harmonic_bond_force.getNumBonds()):
        a, b, length, k = harmonic_bond_force.getBondParameters(i)
        tup = canonicalize_order((a, b))
        omm_bond_params[tup] = (length, k)
        omm_pair_inds.append(tup)

    # assert that I'm defining bonds on the same pairs of atoms
    assert ((set(omm_pair_inds) == set([tuple(p) for p in pair_inds])))

    # What if I initialize with MM parameters
    for i in range(len(pair_inds)):
        length, k = omm_bond_params[tuple(pair_inds[i])]
        length_, k_ = length / unit.nanometer, k / (unit.kilojoule_per_mole / (unit.nanometer ** 2))
        multiplicative_noise = 2 * noise_magnitude * (
                    onp.random.rand(2) - 0.5) + 1.0  # uniform between [1-noise_magnitude, 1+noise_magnitude]
        bond_params[bond_inds[i]] = k_ * multiplicative_noise[0]
        bond_params[bond_inds[i] + n_unique_bonds] = length_ * multiplicative_noise[1]

    def unpack(params):
        return (params, [], [])

    return bond_params, unpack


if __name__ == '__main__':
    # look at a single molecule first
    name = 'AlkEthOH_r4'
    offmol = offmols[name]
    # params, unpack = initialize_off_params(offmol)
    params, unpack = initialize_bonds(offmol, noise_magnitude=1.0)
    pair_inds, bond_inds = get_unique_bonds(offmol)

    # targets
    mm_components, ani1ccx_forces = get_force_targets(name, MMComponents(bonds=True))
    target = mm_components.bonds

    # trajectory
    traj, _, _ = get_snapshots_energies_and_forces(name)
    xyz = traj.xyz


    @jit
    def compute_harmonic_bond_force(xyz, bond_params, pair_inds, bond_inds):
        total_U = lambda xyz: np.sum(compute_harmonic_bond_potential(xyz, bond_params, pair_inds, bond_inds))
        return - grad(total_U)(xyz)


    @jit
    def predict(all_params):
        bond_params, angle_params, torsion_params = unpack(all_params)
        F_bond = compute_harmonic_bond_force(xyz, bond_params, pair_inds, bond_inds)
        return F_bond


    def stddev_loss(predicted, actual):
        return np.std(predicted - actual)


    def rmse_loss(predicted, actual):
        return np.sqrt(np.mean((predicted - actual) ** 2))


    @jit
    def loss(all_params):
        """choices available here:
            * std vs. rmse loss
            * regularization vs. no regularization
            * different normalizations and scalings
        """
        return rmse_loss(predict(all_params), target)


    print('loss at initialization: {:.3f}'.format(loss(params)))

    # check timing
    g = grad(loss)
    t0 = time()
    _ = g(params)
    t1 = time()
    _ = g(params)
    t2 = time()
    print(f'time to compile gradient: {t1 - t0:.3f}s')
    print(f'time to compute gradient: {t2 - t1:.3f}s')

    # TODO: an optimization result class...
    # optimize, storing some stuff
    traj = [params]


    def callback(x):
        global traj
        traj.append(x)

    stop_thresh = 1e-3
    def bh_callback(x, bh_e=None, bh_accept=None):
        L = loss(x)
        print('loss: {:.5f}'.format(L))
        if L <= stop_thresh:
            print('stopping threshold reached ({:.5f} <= {:.5f}), terminating early'.format(L ,stop_thresh))
            return True


    method = 'BFGS'

    fun, jac = jax_play_nice_with_scipy(loss)
    min_options = dict(disp=True, maxiter=500)
    # opt_result = minimize(fun, x0=params, jac=jac, method=method,
    #                      options=min_options, callback=callback)

    # fictitious "temperature" -- from scipy.optimize.basinhopping documentation:
    #   The “temperature” parameter for the accept or reject criterion.
    #   Higher “temperatures” mean that larger jumps in function value will be accepted.
    #   For best results T should be comparable to the separation (in function value) between local minima.
    bh_temperature = 1.0

    opt_result = basinhopping(fun, params, T=bh_temperature,
                              minimizer_kwargs=dict(method=method, jac=jac, callback=callback, options=min_options),
                              callback=bh_callback)

    plot_residuals(predict(opt_result.x), target, name, 'bond')

    loss_traj = [fun(theta) for theta in traj]
    running_min_loss_traj = onp.minimum.accumulate(loss_traj)
    plt.plot(running_min_loss_traj)
    plt.xscale('log')
    plt.yscale('log')
    plt.xlabel(f'{method} iteration (within basin-hopping)')
    plt.ylabel('running minimum of RMSE loss\n(predicted MM bond force vs. OFF1.0 bond force, in kJ/mol / nm)')
    plt.title(f'{name}: bond force regression')
    plt.savefig(f'plots/{name}_bond_loss_traj.png', bbox_inches='tight', dpi=300)
    plt.close()
