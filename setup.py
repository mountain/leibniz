import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="leibniz",
    version="0.2.0",
    author="Mingli Yuan",
    author_email="mingli.yuan@gmail.com",
    description="Leibniz: a package providing facilities to express learnable differential equations based on PyTorch",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/mountain/leibniz",
    project_urls={
        "Documentation": "https://github.com/mountain/leibniz",
        "Source": "https://github.com/mountain/leibniz",
        "Tracker": "https://github.com/mountain/leibniz/issues",
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    packages=setuptools.find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "cached_property",
        "torchpwl",
        "torch",
        "numpy",
    ],
    test_suite="pytest",
    tests_require=["pytest"],
)
