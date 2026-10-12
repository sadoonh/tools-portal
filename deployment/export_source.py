#!/usr/bin/env python3
"""Optional source-only transfer bundle. Deployment itself uses Terraform directly."""
import argparse
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source_files(root=ROOT):
    # Explicit source allowlist: no state, credentials, local tfvars or history.
    names = [
        "app.py", "Dockerfile", "requirements.txt", ".dockerignore", ".gitignore",
        "portal/templates/dashboard.html", "test_app.py", "test_marimohub.py", "test_deployment.py",
        "deployment/export_source.py", "docs/company-deployment.md", "marimohub/build_images.py",
        "marimohub/hub/Dockerfile", "infra/.terraform.lock.hcl", "infra/company.tfvars.example",
    ]
    names += ["infra/" + name for name in (
        "main.tf", "network.tf", "edge.tf", "identity.tf", "variables.tf", "outputs.tf",
        "versions.tf", "marimohub.tf", "marimohub_variables.tf", "marimohub_outputs.tf",
    )]
    names += ["marimohub/kernel/" + name for name in (
        "Dockerfile", "LICENSE", "fargate_agent.py", "requirements.in", "requirements.txt",
        "smoke_notebook.py", "smoke_runtime.py",
    )]
    for name in names:
        path = root / name
        if path.is_symlink() or not path.is_file() or root.resolve() not in path.resolve().parents:
            raise ValueError(f"Missing or unsafe source file: {name}")
    return sorted(names)


def export_source(destination, root=ROOT):
    destination = Path(destination).resolve()
    if destination.exists() or destination == root.resolve() or root.resolve() in destination.parents:
        raise ValueError("Use a new bundle path outside the source checkout")
    names = source_files(root)
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}

    def metadata(member):
        member.uid = member.gid = 0
        member.uname = member.gname = ""
        member.mode, member.mtime = 0o644, 0
        return member

    with destination.open("xb") as stream:
        os.chmod(destination, 0o600)
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name in names:
                archive.add(root / name, arcname=name, recursive=False, filter=metadata)
            archive.add(root / "docs/company-deployment.md", arcname="README.md", recursive=False, filter=metadata)
            data = (json.dumps(hashes, indent=2) + "\n").encode()
            member = tarfile.TarInfo("SOURCE_SHA256.json")
            member.size, member.mode = len(data), 0o644
            archive.addfile(member, io.BytesIO(data))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("Source bundle SHA-256:", export_source(args.output))
