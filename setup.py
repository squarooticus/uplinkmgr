from setuptools import setup, find_packages

setup(
    name="uplinkmgr",
    version="0.9",
    packages=find_packages("src"),
    package_dir={"": "src"},
)
