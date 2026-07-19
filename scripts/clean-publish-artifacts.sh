#!/bin/bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
cd "${ROOT_DIR}"

echo "Cleaning local-only publish artifacts..."

rm -rf output-vmware-iso
rm -rf prototype
rm -rf docs/__pycache__
rm -rf vis/__pycache__ tests/__pycache__

find . -name ".DS_Store" -type f -delete
find . -name "*.pyc" -type f -delete
find . -name ".tmp*" -type f -delete

echo "Done. Publishable docs under docs/ are preserved."
