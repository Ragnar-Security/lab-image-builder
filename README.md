# lab-image-builder

Builds VM images for lab environments from a recipe, on Proxmox, AWS, GCP or
Azure (the Azure backend has run live for Linux; no Windows image has been
built on Azure yet).
It is plain bash plus each platform's own CLI: no Packer, Vagrant, Ansible or
Terraform, and no disk conversion. Each backend builds from its own base, so
one recipe yields one native image per platform.

A build starts a build VM from the backend's base, uploads the recipe's
payload, runs the recipe's setup and verify scripts, seals the VM, saves the
image, destroys the build VM and prints the `templates.yaml` lines for the
new image. Only a build that passed verify and seal is saved.

The full runbook (config keys, recipe keys, the backend contract, tags,
reaping, building the Proxmox base templates) is
[`scripts/image_build/README.md`](scripts/image_build/README.md).

## Recipes live in the repos that use them

This repo holds the builder only. No recipe, payload, host, account, project
or image ID is in it. A recipe is a `.env` file inside the git repository
whose content it builds, next to the payload it uploads: a consuming
repository keeps its own recipes and bases next to its content. The image
name carries the short commit of that repository, so a recipe must be
committed before it is built.

## Quick start

Requirements on the machine that runs a build: bash, git, tar, OpenSSH, and
`python3` for the Proxmox backend, the AWS CLI v2 for AWS, `gcloud` for GCP,
`az` for Azure.

```bash
cp scripts/image_build/config.env.example scripts/image_build/config.env
# Fill in only the section for your backend. config.env is untracked.

# Always dry-run first: prints every command a build would run, runs none.
scripts/image_build/build_image.sh --dry-run path/to/recipe.env aws

scripts/image_build/build_image.sh path/to/recipe.env aws

# Remove expired build VMs a killed build left behind.
scripts/image_build/build_image.sh --reap aws --dry-run
scripts/image_build/build_image.sh --reap aws
```

The backend is `proxmox`, `aws`, `gcp` or `azure`.

## `scripts/sync_template.sh`

The Proxmox backend calls `scripts/sync_template.sh` to copy each built
template to the replica node. This is a copy of the same script in
Ragnar-Lab-Playground, which keeps its own for operators who sync templates by
hand. A change to one must be made to the other.

## Tests

The tests run every backend against fake CLIs; they reach no host or cloud.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pytest==9.0.3
python -m pytest
```

They need Linux (or another POSIX system) with bash, git, tar and python3.

## History

The builder moved out of
[Ragnar-Lab-Playground](https://github.com/Ragnar-Security/Ragnar-Lab-Playground)
at commit `070daad`. Its history up to the move is in that repo.

## License

Apache License 2.0; see [LICENSE](LICENSE).
