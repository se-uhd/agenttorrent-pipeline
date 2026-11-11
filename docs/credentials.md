# Credentials

The pipeline calls GitHub with GitHub access tokens, and OpenRouter, which serves the
classification's models, with OpenRouter API keys. It calls OpenAI only for an OpenAI model that a
command names, such as a reference model of the pilots, and that call needs an OpenAI API key.
`agentcontext/credentials.py` reads the credentials of the three services from the environment and
from one file, `credentials.csv`. `agentcontext/github_api.py` contains the code that every step
that calls GitHub's API shares: the pool of GitHub access tokens and the reading of GitHub's
responses.

## The file

`credentials.csv` contains one credential per row under a header:

```
service,account,credential
github,first-account,github_pat_...
github,second-account,github_pat_...
openrouter,se-uhd,sk-or-v1-...
```

- `service` is `github` or `openrouter`, or `openai` for an OpenAI model that a command names. A
  GitHub access token consists of letters, digits and underscores. An OpenAI API key starts with
  `sk-` and contains hyphens, and an OpenRouter API key starts with `sk-or-`. The reader takes no
  key that starts with `sk-or-` as an OpenAI API key, so that an OpenRouter API key in a row of
  `openai` is a problem rather than a key that OpenAI rejects at the first request. The reader
  skips a row with a credential that does not have the form of its service and returns the problem,
  which the command logs.
- `account` names the owner of the credential: the GitHub account, the OpenAI organization or
  project, or the OpenRouter account. GitHub applies its rate limits per account and OpenAI per
  organization or project, and OpenRouter tracks credit per account and per key. Two GitHub
  access tokens of one account therefore share one rate limit. The field may be empty, and a GitHub
  access token without an account counts as an account of its own.
- The header is the first line of the file. A file that begins with anything else, a comment
  included, is a problem, and the reader returns none of its credentials.
- A line below the header that starts with `#` is a comment, such as an example row that is not
  filled in. The reader drops a byte-order mark and accepts CRLF line ends, because an editor on
  Windows writes both.
- A GitHub access token with read access to public repositories is enough.
- The log message for a problem names the line number and the reason. It never quotes the line
  or one of its cells, because both can contain a credential. A line with a comment after the
  credential is such a line, and in a row with its columns swapped, such as
  `github_pat_...,first-account,github`, the cell of the service is the credential. The runner's
  pre-checks log every problem at every Start into `run_logs/`, which the handover contains, and
  an operator exports the log and sends it on.

The file is never committed. `.gitignore` names `credentials.csv` in every directory, and
`tests/test_credentials.py` checks that `.gitignore` does.

## Reading the credentials

The file is at the repository root, which every stage reaches, or at the path that
`AGENTCONTEXT_CREDENTIALS` names. The container's entrypoint sets that variable to the file
mounted from the project folder, `/app/credentials.csv` (`docs/deploy.md`).

The environment comes first: `GITHUB_TOKENS` (comma-separated) and `GITHUB_TOKEN` for GitHub,
`OPENROUTER_API_KEY` for OpenRouter and `OPENAI_API_KEY` for an OpenAI model that a command names.
The file's rows follow,
without repeats. A GitHub access token that
appears in both keeps the account that the file names. The runner's pre-checks set
`GITHUB_TOKEN`, which every later step inherits, to the first GitHub access token that GitHub
accepts. Without its account, the pool would not pause that GitHub access token together with the
other GitHub access tokens of its account.

A library function reads the file only at the path that it is given. The runner's `run` and the
scripts' `run` functions take a `credentials_path`, and only a command line passes
`credentials.default_path()`. A suite that passes no path therefore never reads a developer's real
file. In addition, `tests/__init__.py` points `AGENTCONTEXT_CREDENTIALS` at `/dev/null` for every
suite under `tests/`.

A value of the environment has to have the form of its service, as a row of the file does. A
value that does not, such as two GitHub access tokens separated by a line break, is not used.
Sent in a request, it would make `requests` refuse the header and quote the value in its error,
and a step such as the GitHub query would log that error for every batch.
`environment_problems` names such a value by its variable and its position, never by its text,
and the runner's pre-checks log the problem as a WARNING.

| Reader | What it takes |
|---|---|
| The runner's pre-checks | Every GitHub access token, checked in order until GitHub accepts one. Repository cloning and the file history step clone with the first GitHub access token that GitHub accepts. The first OpenRouter API key for the labeling's default model when the labeling is pending or can become pending. With it, the pre-checks send one request that reads the credit of the key and spends nothing ([the runner](runner.md#pre-checks)). When the environment and the file hold more than one OpenRouter API key, a WARNING names how many of them are unused, because the pre-checks and the labeling use only the first. The first OpenAI API key only when a command gives the labeling an OpenAI model, with which the pre-checks send one request. A credential in the file counts for a step that declares `GITHUB_TOKENS`, `GITHUB_TOKEN`, `OPENROUTER_API_KEY` or `OPENAI_API_KEY`, and the labeling declares `OPENROUTER_API_KEY`. The pre-checks log a problem with the file or with a value of the environment as a WARNING |
| `1_sampling/2_query_github.py` | Every GitHub access token, pooled by account |
| `snapshot validation`, `measure sizes` | Every GitHub access token, through `--credentials` |
| `2_classification/1_classify_repos.py` | Every GitHub access token, pooled by account, and the first API key of the service of the classifying model: the first OpenRouter API key for the default model or another model that OpenRouter serves, with which a run also reads the credit of the key, or the first OpenAI API key for an OpenAI model that a command names |
| `2_classification/changed_labels.py` | As `1_classify_repos.py`, for the repositories that it labels again |
| `2_classification/2_evaluate_labels.py` | Every GitHub access token, pooled by account, for the READMEs that its review page shows |
| `2_classification/classification_tools.py` | Every GitHub access token, for the outline of the files, a linked page on GitHub, the repository's metadata and the activity of its issues and pull requests. The first API key of the summary model's service, OpenAI's or OpenRouter's, for the summaries of linked pages |

## The pool of GitHub access tokens

`github_api.TokenPool` hands out the GitHub access tokens in turn. For each account, it keeps the
remaining hourly rate limit and the time until which GitHub paused the account. GitHub applies a
secondary rate limit to the user of a GitHub access token. When GitHub pauses one GitHub access
token, the pool therefore sets aside every GitHub access token of that account, for every request
in flight. If each request waited out the limit on its own, the other requests would reach the
limit too.
When every account is paused or spent, `take` waits until the earliest account is available again, or until an event that its caller passes is set. After the event, it returns None. The GitHub query passes the event that ends its pass early, so that it sends no request after the pause when nothing would record the response.

`rate_limit_wait` reads a response as a rate limit only when GitHub marks it as one: `Retry-After`,
`X-RateLimit-Remaining: 0`, or "rate limit" in the message. GitHub returns 403 "Repository access
blocked" for a repository that it took down for a terms-of-service violation, and 451 after a DMCA
takedown, and `blocked` recognizes both. Read as a rate limit, such a 403 would pause the GitHub
access token for good. `rate_limited` recognizes the rate limit that GraphQL reports
with HTTP 200. `graphql_errors` returns the `errors` of a GraphQL body as a list of objects, and
it turns any other value, such as a proxy's text, into the message of an error object.
`rate_limited` and the readers of the classification's queries read the errors through
`graphql_errors`. A reader that called `.get` on a string would raise AttributeError and end the
labeling at every resume that gets the same body. The module imports no HTTP client and reads a
response through `status_code`, `headers` and `json()`, so that the package needs no third-party
dependency for it.

## Tests

`tests/test_credentials.py` checks the file's format, its problems, the order of the environment
and the file, and that the file is never committed. `tests/test_github_api.py` checks the pool's
turns, pauses and rate limits by account, and the reading of a response.
