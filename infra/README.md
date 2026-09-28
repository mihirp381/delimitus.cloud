# infra

Pulumi in Python. Kept out of the uv workspace on purpose: it pulls a cloud provider package that is only chosen after SSC-001 signs the bake-off. Add the provider (`pulumi-gcp`, `pulumi-aws` or `pulumi-azure-native`) then.

```
cd infra && uv sync && uv run pulumi preview
```
