"""
Modal app definition for running Foundry (`forge test`) inside sandboxes.

Usage (from repo root):

    modal deploy agents/modal_foundry_app.py

This creates (or updates) a Modal app named "partyhat-foundry-tests"
with an image that has Foundry installed. The Testing Agent's
`run_foundry_tests` tool is configured to look up this app name via
the MODAL_APP_NAME environment variable (defaulting to the same string),
and then run:

    forge test ...

inside a `modal.Sandbox`.

You are responsible for ensuring that your Foundry project files are
available inside the sandbox at the path you pass as `project_root`
to `run_foundry_tests` (or via FOUNDRY_PROJECT_ROOT). You can achieve
this using Modal Volumes, directory snapshots, or by baking the project
into a custom image.
"""

import modal


BASE_IMAGE = modal.Image.debian_slim().apt_install(
    "curl",
    "git",
    "build-essential",
)


foundry_image = (
    BASE_IMAGE
    .run_commands(
        "curl -L https://foundry.paradigm.xyz | bash",
        "/root/.foundry/bin/foundryup",
        "mkdir -p /opt/foundry-deps",
        "git clone --depth 1 https://github.com/foundry-rs/forge-std /opt/foundry-deps/forge-std",
        "git clone --depth 1 https://github.com/OpenZeppelin/openzeppelin-contracts /opt/foundry-deps/openzeppelin-contracts",
        "git clone --depth 1 https://github.com/smartcontractkit/chainlink-evm /opt/foundry-deps/chainlink-evm",
    )
    .env({
        "PATH": "/root/.foundry/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    })
)


app = modal.App(
    "partyhat-foundry-tests",
    image=foundry_image,
)


@app.function()
def print_forge_version() -> None:
    """
    Simple helper to verify that Foundry is installed in this image.

    Run:
        modal run agents/modal_foundry_app.py::print_forge_version
    """
    import subprocess

    result = subprocess.run(
        ["bash", "-lc", "forge --version && which forge"],
        capture_output=True,
        text=True,
    )
    print("STDOUT:")
    print(result.stdout)
    print("STDERR:")
    print(result.stderr)

