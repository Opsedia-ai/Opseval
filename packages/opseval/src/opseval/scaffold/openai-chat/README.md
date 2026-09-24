# Opseval catalogue

Created with `opseval init`. Next steps:

1. Copy `.env.example` to `.env` and set `OPSEVAL_ALLOWED_HOSTS` (plus any keys).
2. Check the catalogue offline:

   ```bash
   opseval validate suites/smoke.yaml --profile profile.yaml
   ```

3. Run it against your agent:

   ```bash
   opseval run suites/smoke.yaml --profile profile.yaml \
       --transport openai-chat --target https://your-agent.example.com/v1 --junit junit.xml
   ```

   Exit codes: `0` pass · `1` fail · `2` not judged · `3` harness or configuration error.

4. Add your own scenarios under `scenarios/`. Scenarios in `scenarios/llm/` use an
   LLM-played customer and an LLM judge; run them with `suites/full.yaml`.

See `opseval list-scorers` for every available check.
