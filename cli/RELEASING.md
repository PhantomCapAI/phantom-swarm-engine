# Releasing `phantom-bundler` to PyPI

The CLI publishes from `cli/` via the [`publish-cli`](../.github/workflows/publish-cli.yml)
workflow. It builds an sdist + wheel, validates them with `twine check`, and
uploads to PyPI using **Trusted Publishing** (OIDC) — no API token is stored in
the repo.

## One-time setup (PyPI side — repo maintainer)

Trusted Publishing lets GitHub Actions upload without a long-lived token. Because
`phantom-bundler` doesn't exist on PyPI yet, register it as a **pending
publisher** (this also reserves the name):

1. Sign in at <https://pypi.org> → **Your account → Publishing**.
2. Under **Add a new pending publisher**, enter:

   | Field | Value |
   | --- | --- |
   | PyPI Project Name | `phantom-bundler` |
   | Owner | `PhantomCapAI` |
   | Repository name | `phantom-swarm-engine` |
   | Workflow name | `publish-cli.yml` |
   | Environment name | `pypi` |

3. Save. The first successful workflow run creates the project and claims the name.

Optionally create a matching **`pypi`** environment under the repo's
**Settings → Environments** to gate releases behind a required reviewer.

### Token alternative

Prefer an API token? In `publish-cli.yml`, drop the `permissions:` and
`environment:` blocks, add `PYPI_API_TOKEN` as a repository secret, and pass
`with: { password: ${{ secrets.PYPI_API_TOKEN }} }` to the publish step.

## Cutting a release

1. Bump `version` in `cli/pyproject.toml` and `phantom_bundler/__init__.py`
   (keep them in sync).
2. Commit to `master`.
3. Tag and push — the tag is namespaced so it never collides with an engine tag:

   ```bash
   git tag cli-v1.0.0
   git push origin cli-v1.0.0
   ```

4. The `publish-cli` workflow builds, checks, and uploads to PyPI. Confirm at
   <https://pypi.org/project/phantom-bundler/>.

### Dry run on TestPyPI

Trigger the workflow manually (**Actions → publish-cli → Run workflow**) and pick
`testpypi`. Requires a matching pending publisher on <https://test.pypi.org>.

## Local build check

```bash
python -m build cli
twine check cli/dist/*
```
