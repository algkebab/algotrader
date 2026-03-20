import os
import sys

# Add project root so `shared` is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Add filter service so its main module is importable without a package install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "filter"))
