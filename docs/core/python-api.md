---
layout: default
title: Python API
parent: streamwright
nav_order: 7
permalink: /core/python-api/
---

# From Python

Validate and run a source from Python instead of the `streamwright` command.

```python
from streamwright.core.engine.runner import SourceRunner
from streamwright.core.outputs.output import SingerOutput
from streamwright.core.validation.engine import validate_source
from streamwright.core.config.loader import load_source

path = "examples/sources/readers/files_demo"          # a source folder, or a source file (needs streamwright-files)
errors = [issue for issue in validate_source(path) if issue.severity == "error"]
assert not errors, errors                             # streamwright run refuses invalid sources the same way
runner = SourceRunner(load_source(path), config={"data_root": path + "/data"}, secrets={},
                      output=SingerOutput())
state = runner.run()                                  # or run(["orders"]) for some streams
```

- **Secrets:** pass them as `secrets={...}`. For example, `examples/sources/ads/google_ads` takes `developer_token`,
  `client_id`, `client_secret` and `refresh_token`.
- **More entry points:** the full list (validation, loading, running, outputs) is in the
  [API reference]({{ site.baseurl }}/api-reference/#python).
