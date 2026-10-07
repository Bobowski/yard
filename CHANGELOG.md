# Changelog

## 0.1.1

- An update keeps the running container until the new image answers `/health`.
- The swap no longer passes `--group-add keep-groups` to remote Podman.

## 0.1.0

- First release of the `yard` command.
- The PyPI package name is `staryard`.
- `yard --version` prints this version.
