# The calibration of the classification, October 2026

A hash in this record names a commit of the private repository `se-uhd/agenttorrent-pipeline-internal`, which holds the history of this tree, and no check resolves it.

This record keeps the measurements of 2026-10-06 to 2026-10-09 from which the reading rules of the
classification were set: which links the model may follow, how a page is passed to it, how much of
a text it receives, and how the repository's files are shown. The rules themselves are listed in
[the classification's README](../../2_classification/0_README.md#the-reading-rules), which the code
and the specs cite by number, and `2_classification/classify-repos-spec.md` describes the whole
input of the classification. Later commits gave the model inputs that no rule here bounds: GitHub's
description and a tool for what GitHub records about the repository (`40d14497`), the activity
counts of the sampling frame (`3a72d66e`) and a tool for the repository's issues and pull requests
(`60a9af8f`). A heading's "decided" or "proposed" is the state of the rule when it was measured.

## The calibration sample

All measurements come from one simple random sample of 2,282 repositories in the sampling frame
of October 2026 (45,849 repositories, retrieval `2026-10-05T101430Z`), drawn with seed
1983710540. With the correction for the finite population, its size gives a share measured over
the sample a 95 % confidence interval of at most ±2 percentage points for the whole sampling
frame. A share of 1 % rests on about 23 repositories. The evaluation sample is drawn from the
other repositories in the sampling frame, so the label evaluation does not test the classification
on the repositories that its rules were set from.

The measurement called no model. For each repository it read:

- the README through GitHub's README API at the commit that the sampling frame recorded;
- every link of the README: 63,749 distinct links, of which 21,221 lead to github.com and were
  read through GitHub (rule 4) and the rest were fetched as web pages, with every redirect
  and meta refresh followed and no limit on size;
- the list of every file at that commit (GitHub's git trees API) and the bytes per language
  (GitHub's languages API);
- a clone of that commit, one commit deep, and GitHub Linguist over it.

`data/classification/calibration/` contains the results, and `2_classification/diagnostics/` the
scripts: `calibration_measure.py` measures, `calibration_analyze.py` computes the distributions
below, `calibration_summaries.py` runs the summary experiment of rule 10, and
`calibration_language_variants.py` the tests of rule 2, which `language_test.csv` and
`language_check.csv` record. The outline of rule 11 is `outline` in
`2_classification/classification_tools.py`, and the measurement of Linguist over a clone, which
rule 11 replaced, is in `linguist.jsonl.xz`.
Bracketed ranges below are 95 % confidence intervals (Wilson) for the share in the whole sampling
frame.

## Limits of the model

| Constraint | Value | Source |
|---|---|---|
| Context window of `gpt-6-luna` | 1,050,000 tokens | [Model page](https://developers.openai.com/api/docs/models/gpt-6-luna) |
| Maximum output | 128,000 tokens | Model page |
| Room to leave for reasoning and output | "at least 25,000 tokens" | [Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning) |
| Price boundary | above 272,000 input tokens a request costs the long context price, $0.20 instead of $0.10 per million input tokens | [Pricing](https://developers.openai.com/api/docs/pricing), 2026-10-06 |
| Characters per token | median 4.03, 10th percentile 3.57 | 100 READMEs of the sample, counted with OpenAI's token counting endpoint (`responses.input_tokens.count`) |

OpenAI gives no share of the window to use. Its guidance on long inputs is qualitative and
written for earlier models: the GPT-4.1 prompting guide reports that performance degrades when
many items must be retrieved from a long context, and the GPT-5.2 prompting guide recommends
summarizing inputs longer than about 10,000 tokens.

## The README

### Rule 1: how much the model reads in one request (decided)

A request contains the prompt, the README and the outputs of the tools that the model called.
Over the 2,212 READMEs that the classification can use, the length in characters:

| Median | 90th | 99th | Maximum |
|---|---|---|---|
| 5,852 | 21,064 | 64,119 | 406,750 |

With nothing cut, the prompt, the whole README and the whole outline of rule 11, which lists
every file of every practice, come to a median of 3,793 tokens at 3.57 characters per token, a
99th percentile of 31,847 and a maximum of 321,767. One request of the 2,282 crosses 272,000
tokens. The model receives pages as summaries of at most about 1,000 characters (rule 10).

Decided: Nothing is cut while a request stays under 272,000 input tokens, the price boundary,
which also lies far inside the window and its reserve. A request that would cross it is cut in
this order, and a line in the request marks each cut: the outline's lists of files per practice,
then the README. The batch classification of March 2026 encountered a README of 6.7 million
characters, which this rule cuts.

### Rule 2: the language of the README (decided)

Before this rule, the classification excluded a repository with a README that is not in English
before a model read it. The check comes from the batch classification of March 2026
(`1_collect_repo_data.py`, removed in `b47c1bf1`) and cites no source. Text that is ASCII to 90 %
counts as English, and otherwise Lingua decides with a confidence of 0.70, or 0.50 for fewer than
60 letters. Over the sample:

- The ASCII rule decides 2,206 of the 2,277 READMEs. It lets 12 READMEs in French, Portuguese,
  German, Polish and Indonesian through (0.5 %), which the model was meant to label `false` under
  the prompt. Without the ASCII rule, Lingua alone would exclude 13 READMEs in English that it
  misclassifies, such as CyberChef as Yoruba.
- The check excludes 65 READMEs (2.9 %), 52 of them in Chinese. 21 of the 65 link an English
  README of the same project, such as alibaba/transmittable-thread-local and
  PaddlePaddle/PaddleNLP. The README API returns the README that GitHub shows by default, and the
  classification does not follow a link to another README.

Decided: The check is dropped. The default README, which GitHub shows on the repository's page,
decides the language, and a README in another language that it links to does not change it. The
model applies the prompt's rule to the default README, at about 65 more requests per 2,282
repositories, about 3 cents.

The test of 2026-10-06 classified the 77 READMEs in other languages (the 12 that the check let
through and the 65 that it excluded) and the 13 English READMEs that Lingua misclassifies, three
times, at about 7 cents a run. A README in another language is labeled `false` as follows:

| What the model is given | Other languages labeled `false` | English labeled `false` for the language |
|---|---|---|
| The prompt as it is: the rule is one of the exclusion criteria | 4 of 77, none for the language | 0 of 13 |
| The rule stated in the task as well: "Only a README mainly written in English qualifies..." | 72 of 77 | 0 of 13 |
| A field of the response, `readme_mainly_english`, and the classification labels `false` every README for which the field is false | 77 of 77 | 0 of 13 |

The model judges the language correctly. With the field, it reports all 77 as not mainly English
and all 13 as English. It does not apply the rule to its label, however. Under the prompt as it
is, it labels 11 of the 12 and 62 of the 65 `true` for their engineering, and even with the field
its own label is `false` for 33 of the 77. Under the rule stated in the task, 5 of the 65 stay
`true`, such as xuxueli/xxl-conf, with a README that opens with one paragraph in English and
continues in Chinese. `language_test.csv` contains every label of the
three runs.

Decided on 2026-10-06: The language check and the model are combined. The check runs first and
excludes a README that it finds not to be in English, without a request to the model. Every other
README goes to the model. The model's response contains `readme_mainly_english`, and the
classification labels `false` every README for which the field is false. The decision records the
model's own label, the field and the measures that the check read. The thresholds are set by
measurement. The classification ran over the whole calibration sample with the field, at about
$1.94, and recorded Lingua's measures for each README, and the check was computed afterwards.
`language_check.csv` records each README's measures, the check, the
field and the labels.

| Of the 2,277 READMEs with text | Model: mainly English | Model: not mainly English |
|---|---|---|
| Check: English | 2,198 | 14 |
| Check: not English | 0 | 65 |

The check never excludes a README that the model judges English. The model judges all 65 READMEs
that the check excludes not mainly English, a share of at least 94.4 % at 95 % confidence
(Wilson). Of the 14 that the check lets through, 12 are the READMEs in French, Portuguese,
German, Polish and Indonesian above, written in ASCII, for which Lingua identifies the language
correctly. The other two are bgerp/bgerp, with each paragraph in English and then in Bulgarian,
and NiREvil/vless, written in Persian with English names, which Lingua classifies as English. Read
by a person, neither is mainly English. The batch classification's thresholds stay. They exclude
no README that the model judges English, and catching the 14 by the check would mean fitting
thresholds to 25 READMEs (the 12 above and the 13 English READMEs that Lingua misclassifies),
while the model catches the 14 for about $0.001 each. The model's own label contradicted its
judgment for 43 of the 79.

An offline test on 2026-10-06 checked whether another check would catch the 14 without excluding
an English README. It read the same 2,277 READMEs again at their commits and took the model's
judgment as the reference (`calibration_language_variants.py`):

| Check | English READMEs that it excludes | Not mainly English, let through |
|---|---|---|
| The batch classification's (kept) | 0 | 14 |
| Lingua over all its languages, without the ASCII rule | 13 | 2 |
| Lingua over English and the 10 languages of the 79, without the ASCII rule | 7 | 2 |
| The share of the prose that Lingua assigns to English, at least half | 81 | 0 |

Every other check excludes English READMEs, most of them short. An excluded README is never sent
to the model, while a README that the check lets through costs a request and is still labeled
`false`. The 14 are about 0.6 % of the READMEs, about 280 requests and $0.24 for the sampling
frame of October 2026.

## Links

### Rule 3: only links to text (decided)

The reader requests text with the header
`Accept: text/html, application/xhtml+xml, text/plain, text/markdown;q=0.9, */*;q=0.1` and
reads a body only when the response's `Content-Type` is `text/html`, `application/xhtml+xml`,
`text/plain`, `text/markdown`, its older name `text/x-markdown`, or is absent. Servers may ignore
the `Accept` header. The `Content-Type` arrives before the body, so a refused response costs a
request and no download. Of the 58,021 links fetched as web pages, 11,708 (20 %) returned an
image: `image/svg+xml` 8,331, `image/png` 2,292, `image/jpeg` 784 and others. The other refused
types were `application/octet-stream` 206, `application/pdf` 133, `application/json` 46, and
archives and videos. Reading PDFs (133, 0.2 % of the links) is not proposed.

### Rule 4: github.com links are read through GitHub (decided)

A third of all links lead to github.com, and GitHub's web page for a file or a repository begins
with several thousand characters of GitHub's own navigation. The reader reads a github.com link
through GitHub's API instead:

| The link names | Read as | Links | Read | Characters: median, 99th percentile |
|---|---|---|---|---|
| a repository | its README (README API) | 5,117 | 4,900 | 5,751; 122,279 |
| a file (`blob`, `raw`) | the file | 5,272 | 4,791 | 2,048; 240,957 |
| a directory (`tree`) | its listing (contents API), or the file for a `tree` link to a file, which GitHub's web page redirects | 2,493 | 2,439 | 299; 8,326 |
| releases | the list of releases (releases API) | 804 | 797 | 1,920; 6,664 |

Links to other pages of github.com are not read: actions 2,118, accounts 1,542, issues 1,054,
wiki 723, commits 566, workflows 243, discussions 177, graphs 146, pull requests 220, and fewer of
each other type.

### Rule 5: relative links are read through GitHub at the recorded commit (decided)

A relative link of a README names a file or a directory of the repository. The measurement read
relative links as files of raw.githubusercontent.com at `HEAD`. 2,143 of them returned 404, and
1,809 of these name a path without a file extension, such as a directory. The file read was also
the version at the current `HEAD` rather than at the commit at which the README was read. A
relative link is read in the same way as a `blob` or `tree` link under rule 4, at the commit that
the sampling frame recorded.

### Rule 6: a meta refresh is followed (decided)

A meta refresh is an HTML page that sends the browser on to another address with
`<meta http-equiv="refresh" content="0; url=…">` in its head, instead of an HTTP redirect. A
documentation site that keeps several versions often serves its root this way. The root of
`ess-dmsc.github.io/h5cpp` contains only "If you are not redirected automatically, follow this
link to v:stable". 438 of the 58,021 fetched pages were meta refreshes. The reader follows a meta
refresh as a redirect.

### Rule 7: at most 5 redirects (decided)

Redirects are counted per link: each response with status 301, 302, 303, 307 or 308 and a
`Location` header, and each meta refresh, until a response that is neither. Of the 56,915 links
that received a response, 126 needed more than 3 and 12 more than 5. Five did not end within 10.
Proposed: a limit of 5, which cuts 12 links, 0.02 % [0.01 %, 0.04 %]. Decided.

### Rule 8: the reader's time limits (decided)

The time from the first request to the end of the last response: median 1 second, 99th
percentile 15 seconds. The reader allows 10 seconds to connect and 30 seconds per read.

### Rule 9: how much of a page is downloaded (decided)

The bytes of a readable page: median 38,446, 99th percentile 1,498,902, maximum 20,121,131. A
limit of 2 MB cuts 0.55 % [0.46 %, 0.65 %], 5 MB cuts 0.10 % [0.07 %, 0.15 %]. Decided: 2 MB.

### Rule 10: the model receives a page as a summary (decided)

The text of a readable page outside github.com, in characters:

| Median | 90th | 99th | Maximum |
|---|---|---|---|
| 4,641 | 27,319 | 204,558 | 20,051,925 |

14.0 % [13.6 %, 14.4 %] are longer than 20,000 characters, about 5,000 tokens, and 4.9 % longer
than 50,000. Many pages begin with navigation, menus and footers. Proposed: The tool that reads
a page returns a summary of it, which a model writes from a summary prompt when the
classification model requests the page. A summary bounds the length and leaves out the
navigation, and OpenAI's guidance for long inputs is to summarize them (above). A summary request
stays under the price boundary. The page text is cut at 960,000 characters, which at 3.57
characters per token keeps it under 272,000 tokens, and 0.22 % of the pages are longer.

The experiment of 2026-10-06 summarized 300 pages drawn at random from the readable pages (seed
1937722935) with `gpt-6-luna` at reasoning effort `low` and the prompt in
`calibration_summaries.py`, which requests at most five sentences on what the page
is, what it states about the software and what evidence of engineering practice it contains:

| | Median | 90th | 99th | Maximum |
|---|---|---|---|---|
| Characters of the page text | 5,241 | 27,940 | 144,976 | 243,405 |
| Characters of the summary | 602 | 765 | 961 | 997 |

The 300 requests cost 1,046,851 input and 41,190 output tokens, $0.125, about $0.0004 a page. All
succeeded and none was cut. 163 summaries state that the page contains nothing about the
software, because the pages were drawn from all links, not only from links that a model requests,
and include backer pages, licenses and other projects' sites. Read side by side with their pages,
the summaries report the content of the page without its navigation.
`summaries.jsonl.xz` contains the pairs.

## The repository's files

### Rule 11: the outline instead of Linguist over a clone (decided)

The outline is a text about the repository's files. It is built without a clone from two of
GitHub's responses for the commit that the sampling frame recorded: the list of every file (git
trees API) and the bytes per language (languages API). It gives the number of files and
directories of the repository and the depth of its deepest path, its languages with their shares,
every top-level directory with its number of files and most common file types, the files at the
root, and the files that show a practice that the prompt names: tests, CI, build and packaging,
documentation, contribution guidelines, changelogs and releases, containers. An example, for
go-vgo/robotgo, shortened at each `...`:

```
Files: 186, directories: 42, deepest path: 5 levels
Languages: Go 85.2%, C 11.0%, Objective-C 3.6%, HTML 0.1%, Dockerfile 0.1%
Top level:
  wayland/ (17 files: .go 17)
  docs/ (8 files: .md 7, .svg 1)
  ...
  .gitignore, AGENTS.md, CONTRIBUTING.md, LICENSE, README.md, go.mod, ...
Paths that show engineering practice:
  tests: 23 files (clipboard/clipboard_darwin_test.go, clipboard/clipboard_test.go, ...)
  ci: 3 files (.circleci/config.yml, .circleci/images/primary/Dockerfile, .github/workflows/go.yml)
  build and packaging: 1 file (go.mod)
  ...
```

Over the sample:

| | Outline | Linguist over a clone |
|---|---|---|
| Characters, median; 99th percentile; maximum | 1,576; 60,153; 1,124,259 (rule 12) | 1,238; 5,733; 9,488 |
| Cost | 2 API requests | a clone: median 4 seconds, 99th percentile 287, maximum 2,176; checkouts up to 4.7 GB |
| Shows tests, CI, packaging, documentation, contribution, changelogs | yes | no |

GitHub truncates the file list of 9 repositories (0.4 %), and the outline marks the truncation.
Known false matches: An application directory named `Test`, as in firefly-iii, counts as tests.

### Rule 12: the outline lists everything (decided)

The outline has no limits of its own. With every top-level directory, every root file and every
language, the longest outline in the sample has 23,046 characters, at most 6,455 tokens. With
every file that shows a practice as well, the longest outline has 1,124,259 characters, which
rule 1 cuts. Over the sample, the whole request crosses 272,000 tokens once. Limits of 60
directories, 60 root files, 10 languages and 3 example files per practice, chosen before this
measurement, are withdrawn.

## Rules that only a model can settle (set by the experiment of 2026-10-06)

### Rule 13: pages per repository, and rule 14: tool rounds per repository (decided)

A README in the sample has a median of 10 links that lead to readable text, 29 at the 90th
percentile and 93 at the 99th. How many of them the model reads is a property of the model.
Decided: Run the classification over 200 repositories drawn at random from the calibration sample
with limits that do not bind, 10 pages and 10 rounds, and set each limit to the largest number
that the model used. The decision records each limit that a repository reaches. The measurement
of rule 2 revised the round limit, as described at the end of this section.

The experiment ran on 2026-10-06 with the rules above implemented, over the 200 repositories in
`experiment_sample.csv` (seed 1699890741). `experiment.csv` records
each repository's label, rounds, pages, outline length, tokens and decision size.

| Per repository | 0 | 1 | 2 | 3 | 5 |
|---|---|---|---|---|---|
| Rounds of tool calls | 28 | 166 | 6 | | |
| Pages read | 172 | 20 | 4 | 3 | 1 |

Decided: at most 2 rounds of tool calls and 5 pages, raised to 5 rounds below. A tool call for a
page past the limit returns a note that the limit is reached. Each tool call of a round past the
limit returns the same note, and the next request sets `tool_choice: "none"`, so that the model
responds without tools and the repository is still decided. The decision lists the limits that it
reached. The measurement of rule 2 ran the whole calibration sample under these limits. 1,773 of the
2,277 labeled repositories took one round of tool calls and 60 took two. For 2 repositories,
FormidableLabs/spectacle and spacebarchat/server, the model requested a third round, and both were
decided without it. In that run, the model read at most 3 pages for any repository. For both
repositories, the model requested the outline of the files in the third round, after two pages, so
the limit withheld evidence rather than stopping a model that called tools without end. Decided: at
most 5 rounds, which none of the 2,477 labels of the two runs reaches, and which still stops a model
that goes on calling tools after 7 requests, at about $0.005 for the repository. The limit of 5
pages stays, because in the experiment the model read at most 5 pages for any repository, which the
limit allows.

The model called a tool for 172 of the 200 repositories, the outline for 168 of them, while the
prototype with `gpt-5.2` and Linguist's breakdown called a tool for 7 of 400. It labeled 188
`true` and 12 `false`, and agrees with the batch classification of March 2026 on 135 of the 140
repositories that the batch classification labeled. The five that differ are
fluiggers/fluig-vscode-extension, with a README in Portuguese (rule 2), okfn/website,
googleapis/repo-automation-bots, aedenthorn/StardewValleyMods and miktaew/yet-another-idle-rpg,
and `experiment.csv` records both labels. The run took 378 requests and 3 minutes 38 seconds with
8 workers: 2,126,275 input tokens, 699,305 of them cached, 22,040 output tokens, and 184,292
input and 4,336 output tokens for the summaries, $0.18 at the standard prices, about $0.0009 a
repository. Six pages could not be read.

A decision keeps every tool output (see Other rules). The run's decisions file contains 1,591
bytes per decision, 86 % of them in the outlines. The longest outline, of FuelLabs/sway, has
623,762 characters. Without the outlines, a decision takes 225 bytes. At that rate, a run over
the 45,849 repositories in the sampling frame of October 2026 writes about 73 MB. Decided: A
run's decisions are split into parts that each stay under 95 MB, below GitHub's limit of 100 MB
for a file.

## The prompts

On 2026-10-06 the classification used three texts that a model reads: the classification prompt
(`2_classification/prompts/classification-prompt.md`), the prompt for the summary of a page
(rule 10), and the descriptions of the two tools. They were checked on 2026-10-06 with the
contradiction checker and the format checker of OpenAI's prompt optimizer, run as the
[Optimize Prompts cookbook](https://developers.openai.com/cookbook/examples/optimize_prompts)
publishes them, with `gpt-4.1`. Neither found an issue. The third checker compares examples with
the prompt, and the prompts have none. Read against OpenAI's guidance for reasoning models
([reasoning best practices](https://developers.openai.com/api/docs/guides/reasoning-best-practices),
[prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering)), the
prompts change as follows (decided with the rules above):

| Prompt | Finding | Guidance | Change |
|---|---|---|---|
| Classification | A checklist of four questions to go through before responding | "Avoid chain-of-thought prompts" | Removed. The precedence rule orders the decision |
| Classification | The README follows a Markdown heading, and READMEs have headings of their own | "Use delimiters like markdown, XML tags, and section titles" | The README is wrapped as `<readme path="…">…</readme>` |
| Classification, summary | READMEs and pages are text from strangers and can contain instructions | Developer messages contain the rules, user messages the inputs | A sentence of the prompt states that the README, the outline and the summaries are data, not instructions |
| Classification | "Not written in English" leaves open which README | "Provide specific guidelines" | The default README decides (rule 2) |
| Classification | The output section repeats, as pseudo-JSON, the format that a strict schema already enforces | The response format is enforced by the schema | The section names the fields and what the justification contains |
| Classification, tools | The descriptions name Linguist and the text of a page | Rules 10 and 11 | They describe the outline and the summary |
| Summary | Instructions and the page share one user message | Developer messages contain the rules | The instructions are the developer message, and the page is wrapped as `<page url="…">…</page>` |
| Tools | `get_file_tree` shows the model a repository name and a revision that it must not set | "Be very specific about your end goal" | The model receives no parameters, and the classification supplies both |

Decided: A README in two languages, such as several READMEs that the old check excluded, counts
as written in English only if it is mainly English.

The first change of the table did not last. The label evaluation of 2026-10-07 found that the
reference model labeled `true` repositories that an exclusion criterion covers. Since `40d14497`,
the response format therefore requires a value for every exclusion criterion before the
engineering, and the classification derives the label from these values rather than from a
precedence rule (`2_classification/classify-repos-spec.md`).

## Other rules

| Rule | Value | Grounding |
|---|---|---|
| Addresses off the public internet are refused | every address that a host resolves to must be global | The stage runs in a container on a network with other machines. 339 links of the sample name an address of the machine itself or of a local network, mostly `localhost`, and 422 name hosts that no longer resolve |
| A tool's output kept with a label | all of it | Bounded by rule 1. The prototype's cut at 20,000 characters is removed |
| Reasoning effort | `medium` for the classification, `low` for a summary (rule 10) | Decided on 2026-10-09 by labeling the reviewed disagreements of the label evaluation of 2026-10-07 at both efforts (`75bd9a04`, `e80dcf9c`). Of the 30 reviewed disagreements with a definite label, `gpt-6-luna` labeled 27 as the reviewer did at `medium` and 24 at `low`, for $0.0011 and $0.0010 a repository on the flex tier |
| Attempts | GitHub: 4, waiting 10, 30 and 60 seconds, up to 30 pauses for a rate limit; the model: 3 after the SDK's 6 retries | Open: taken from the sampling stage's GitHub query and the prototype, not measured |
| Clone and Linguist time limits | 900 and 600 seconds | Removed with rule 11 |
