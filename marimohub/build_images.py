#!/usr/bin/env python3
"""Local Dockerless OCI builds only. No AWS provisioning, credentials or pushing.

Requires Crane, uv, GNU tar and Linux unprivileged user/mount namespaces.
Distro/uv installation runs inside pinned image rootfs, never on the host.
Package managers resolve current security patches; digests/scans are recorded.
"""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_BASE = "python:3.12.15-alpine3.23@sha256:9ce5a4e59b833c002e1c4196c60ec0b38977e3235d626298ba7ffe2e64f5a30c"
NODE_BASE = "node:24.18.0-alpine3.23@sha256:00295958df3ca22b4d3df4ae8dbffd823dbca1c53efbcf235f62fbf3fa5ca756"
UV = "ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded"
HUB = "ghcr.io/marimo-team/marimohub@sha256:e8988391cfd83431da8545a833652edf9496184a673ef865f99749843009ab7f"
AGENT_HASH = "39885ebf326f2255245666a1e428416c54bbfb9809132740442ae73122b9f52d"
KERNEL_ENV = {
    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/home/notebook",
    "UV_PROJECT_ENVIRONMENT": "/opt/marimohub/venv",
    "UV_CACHE_DIR": "/opt/marimohub/uv-cache", "UV_PYTHON_DOWNLOADS": "never",
    "VIRTUAL_ENV": "/opt/marimohub/venv",
    "PATH": "/opt/marimohub/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
    "MARIMO_SKIP_UPDATE_CHECK": "1", "_MARIMO_APP_OVERLOAD_AUTO_DOWNLOAD": "[html]",
}


def run(*args):
    print("+", shlex.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True)


def export_rootfs(image, directory):
    rootfs = directory / "rootfs"
    rootfs.mkdir()
    run("crane", "export", "--platform", "linux/amd64", image, directory / "source-rootfs.tar")
    # Trusted digest-pinned image: GNU tar supports legitimate absolute system
    # symlinks that Python's data-only extraction filter would reject.
    run("tar", "--no-same-owner", "-xf", directory / "source-rootfs.tar", "-C", rootfs)
    # Rootfs reconstruction/package tools can change directory permissions;
    # mkdir's 0755 default must not overwrite standard non-root writable /tmp.
    (rootfs / "tmp").mkdir(exist_ok=True)
    (rootfs / "tmp").chmod(0o1777)
    return rootfs


def install_into_rootfs(rootfs, commands):
    """Temporary DNS/dev mounts and an empty environment; never copy credentials."""
    resolver = rootfs / "etc/resolv.conf"
    original = resolver.read_bytes() if resolver.exists() else None
    shutil.copyfile("/etc/resolv.conf", resolver)
    for device in ("null", "urandom"):
        (rootfs / "dev").mkdir(exist_ok=True)
        (rootfs / "dev" / device).touch()
    namespace = """
root=$1
mount --bind /dev/null "$root/dev/null"
mount --bind /dev/urandom "$root/dev/urandom"
exec chroot "$root" /usr/bin/env -i PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive UV_CACHE_DIR=/opt/marimohub/uv-cache UV_PYTHON_DOWNLOADS=never /bin/sh -ec "$2"
"""
    try:
        run("unshare", "--user", "--map-root-user", "--mount", "--keep-caps", "/bin/sh", "-ec", namespace, "build", rootfs, commands)
    finally:
        if original is None:
            resolver.unlink(missing_ok=True)
        else:
            resolver.write_bytes(original)


def kernel_layer_metadata(member):
    """Public OS files stay root-owned; standard /tmp and scoped user paths work."""
    name = member.name.removeprefix("./").rstrip("/")
    if any(name.startswith(p + "/") for p in ("dev", "proc", "sys", "tmp", "run")):
        return None
    owner_paths = ("opt/marimohub", "home/notebook", "workspace")
    owned = any(name == p or name.startswith(p + "/") for p in owner_paths)
    member.uid = member.gid = 10001 if owned else 0
    member.uname = member.gname = ""
    if name == "tmp":
        if not member.isdir():
            raise ValueError("Kernel /tmp must be a directory")
        member.mode = 0o1777
    return member


def kernel(directory):
    source = ROOT / "marimohub/kernel"
    if hashlib.sha256((source / "fargate_agent.py").read_bytes()).hexdigest() != AGENT_HASH:
        raise ValueError("Vendored agent differs from the approved upstream release")
    rootfs = export_rootfs(KERNEL_BASE, directory)
    run("crane", "export", "--platform", "linux/amd64", UV, directory / "uv.tar")
    with tarfile.open(directory / "uv.tar") as archive:
        for binary in ("uv", "uvx"):
            member = next(m for m in archive.getmembers() if m.name.lstrip("./") == binary)
            target = rootfs / "usr/local/bin" / binary
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(0o755)
    (rootfs / "tmp").mkdir(exist_ok=True)
    shutil.copyfile(source / "requirements.txt", rootfs / "tmp/kernel-requirements.txt")
    install_into_rootfs(rootfs, """
apk upgrade --no-cache
apk add --no-cache git ca-certificates
mkdir -p /opt/marimohub /workspace /home/notebook
uv venv --python /usr/local/bin/python3 /opt/marimohub/venv
uv pip sync --python /opt/marimohub/venv/bin/python /tmp/kernel-requirements.txt --default-index https://pypi.org/simple
rm /tmp/kernel-requirements.txt
""")
    # Single-ID user namespaces cannot chown to an unmapped 10001. Encode the
    # intended owners in OCI tar metadata instead; never weaken to chmod 777.
    for name, entry in (("passwd", "notebook:x:10001:10001:Notebook:/home/notebook:/bin/sh\n"),
                        ("group", "notebook:x:10001:\n")):
        with (rootfs / "etc" / name).open("a") as file:
            file.write(entry)
    profile = rootfs / "etc/profile.d/venv-path.sh"
    profile.write_text('export PATH="$VIRTUAL_ENV/bin:$PATH"\n')
    profile.chmod(0o644)
    for name in ("fargate_agent.py", "LICENSE"):
        shutil.copyfile(source / name, rootfs / "opt/marimohub" / name)
    (rootfs / "opt/marimohub/fargate_agent.py").chmod(0o755)
    (rootfs / "opt/marimohub/LICENSE").chmod(0o644)
    for path in (rootfs / "workspace", rootfs / "home/notebook", rootfs / "opt/marimohub"):
        path.chmod(0o755)

    with tarfile.open(directory / "payload.tar", "w") as archive:
        archive.add(rootfs, arcname=".", filter=kernel_layer_metadata)
    arguments = ["crane", "mutate", "--platform", "linux/amd64", KERNEL_BASE,
                 "--append", directory / "payload.tar", "--user", "10001:10001",
                 "--workdir", "/workspace", "--exposed-ports", "2717,2718",
                 "--cmd", "/usr/local/bin/python3,/opt/marimohub/fargate_agent.py",
                 "--output", directory / "image.tar"]
    for name, value in KERNEL_ENV.items():
        arguments += ["--env", f"{name}={value}"]
    run(*arguments)
    (directory / "runtime-env.json").write_text(json.dumps(KERNEL_ENV, indent=2) + "\n")
    print("Kernel image and rootfs prepared:", directory)


def tree_hashes(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def hub_security(directory):
    upstream = directory / "upstream"
    upstream.mkdir()
    source = export_rootfs(HUB, upstream) / "app"
    rootfs = export_rootfs(NODE_BASE, directory)
    install_into_rootfs(rootfs, "apk upgrade --no-cache")
    shutil.copytree(source, rootfs / "app", symlinks=True)
    assert tree_hashes(source) == tree_hashes(rootfs / "app"), "Official application changed"
    (directory / "application-hashes.json").write_text(json.dumps(tree_hashes(source), indent=2) + "\n")
    (rootfs / "home/hub").mkdir(parents=True)
    for name, entry in (("passwd", "hub:x:10001:10001:Hub:/home/hub:/bin/sh\n"),
                        ("group", "hub:x:10001:\n")):
        with (rootfs / "etc" / name).open("a") as file:
            file.write(entry)

    def metadata(member):
        name = member.name.removeprefix("./").rstrip("/")
        if any(name.startswith(p + "/") for p in ("dev", "proc", "sys", "tmp", "run")):
            return None
        member.uid = member.gid = 10001 if name == "home/hub" or name.startswith("home/hub/") else 0
        member.uname = member.gname = ""
        return member

    with tarfile.open(directory / "payload.tar", "w") as archive:
        archive.add(rootfs, arcname=".", filter=metadata)
    run("crane", "mutate", "--platform", "linux/amd64", NODE_BASE,
        "--append", directory / "payload.tar", "--user", "10001:10001",
        "--workdir", "/app", "--exposed-ports", "3000", "--cmd", "node,dist/index.mjs",
        "--env", "NODE_ENV=production", "--env", "PORT=3000", "--env", "HOME=/home/hub",
        "--env", "MARIMOHUB_STATIC_ROOT=./public", "--env", "MARIMOHUB_VERSION=0.4.16",
        "--env", "MARIMOHUB_IMAGE=marimohub-0.4.16-alpine-security-rebase",
        "--output", directory / "image.tar")
    print("Official application preserved; Alpine hub security rebase prepared:", directory)


def portal(directory):
    # Install inside the pinned runtime (including musl wheels), never from the
    # operator's Python environment. Account/region/URLs/secrets are not baked in.
    rootfs = export_rootfs(KERNEL_BASE, directory)
    shutil.copyfile(ROOT / "requirements.txt", rootfs / "tmp/portal-requirements.txt")
    install_into_rootfs(rootfs, """
apk upgrade --no-cache
python3 -m pip install --only-binary=:all: --no-cache-dir -r /tmp/portal-requirements.txt
rm /tmp/portal-requirements.txt
""")
    (rootfs / "app").mkdir()
    shutil.copyfile(ROOT / "app.py", rootfs / "app/app.py")
    shutil.copytree(ROOT / "portal/templates", rootfs / "app/portal/templates")
    for name, entry in (("passwd", "portal:x:10001:10001:Portal:/nonexistent:/bin/false\n"),
                        ("group", "portal:x:10001:\n")):
        with (rootfs / "etc" / name).open("a") as file:
            file.write(entry)

    def metadata(member):
        name = member.name.removeprefix("./").rstrip("/")
        if any(name.startswith(p + "/") for p in ("dev", "proc", "sys", "tmp", "run")):
            return None
        member.uid = member.gid = 0
        member.uname = member.gname = ""
        return member

    with tarfile.open(directory / "payload.tar", "w") as archive:
        archive.add(rootfs, arcname=".", filter=metadata)
    command = json.loads(next(line[4:] for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("CMD ")))
    run("crane", "mutate", "--platform", "linux/amd64", KERNEL_BASE,
        "--append", directory / "payload.tar", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTHONUNBUFFERED=1", "--workdir", "/app", "--user", "10001:10001",
        "--exposed-ports", "8000", "--cmd", ",".join(command), "--output", directory / "image.tar")
    # Test-only files follow packaging, so they never enter the production image.
    for name in ("test_app.py", "test_marimohub.py"):
        shutil.copyfile(ROOT / name, rootfs / "app" / name)
    (rootfs / "app/marimohub").mkdir()
    shutil.copyfile(ROOT / "marimohub/build_images.py", rootfs / "app/marimohub/build_images.py")
    print("Portal image and rootfs prepared:", directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("kernel", "portal", "hub-rootfs", "hub-security"))
    parser.add_argument("output", type=Path, help="New directory outside the repository")
    args = parser.parse_args()
    directory = args.output.resolve()
    if directory == ROOT or ROOT in directory.parents:
        parser.error("Use a new build directory outside the repository")
    os.umask(0o022)  # Public image assets must be readable by the non-root user.
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if args.kind == "kernel":
        kernel(directory)
    elif args.kind == "portal":
        portal(directory)
    elif args.kind == "hub-security":
        hub_security(directory)
    else:
        export_rootfs(HUB, directory)


if __name__ == "__main__":
    main()
