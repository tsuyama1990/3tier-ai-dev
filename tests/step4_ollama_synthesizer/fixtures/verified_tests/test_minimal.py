from ase import Atoms
from ase.calculators.emt import EMT

def test_atoms_energy():
    atoms = Atoms('N2', positions=[(0, 0, 0), (0, 0, 1.1)])
    atoms.calc = EMT()
    energy = atoms.get_potential_energy()
    assert isinstance(energy, float)
