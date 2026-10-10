# StreamWright - Generic REST API Connector (`restapi`)

The `restapi` connector enables declarative HTTP stream definitions for any REST API service (GitHub, Stripe, Shopify, internal microservices) without having to create a separate bespoke connector package.

## Installation

```bash
pip install streamwright-restapi
```

Or via StreamWright CLI:

```bash
streamwright connectors install restapi
```

## Source Definition Example

```yaml
kind: source
name: github_demo

auth:
  provider: restapi
  type: bearer
  token: "{{ secrets.github_token }}"

http:
  base_url: https://api.github.com

streams:
  - name: stargazers
    requests:
      - name: raw_stargazers
        http:
          path: /repos/org/repo/stargazers
    transform:
      mode: page
      steps:
        - name: stargazers
          select: |
            SELECT record->>'login' AS user_login
            FROM raw_stargazers
    export:
      stargazers:
        step: stargazers
```
