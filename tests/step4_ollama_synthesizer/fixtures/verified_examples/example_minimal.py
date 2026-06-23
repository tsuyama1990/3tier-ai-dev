from ase import Atoms
from ase.io import read, write

atoms = Atoms("H2O", positions=[(0, 0, 0), (0, 0.96, 0), (0.93, -0.24, 0)])
write("water.traj", atoms)
loaded = read("water.traj")
print(f"Loaded {len(loaded)} atoms")
