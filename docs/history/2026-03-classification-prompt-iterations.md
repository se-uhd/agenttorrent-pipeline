# Iterations of the batch prompt

This file keeps the conversation in which the batch prompt of March 2026 was revised, converted
from the document that recorded it.

[<u>https://chatgpt.com/share/696def05-4870-8005-b227-30335c59b8f8</u>](https://chatgpt.com/share/696def05-4870-8005-b227-30335c59b8f8)

I'm working on a prompt that we want to use for classifying GitHub repositories based on their README files. Please check the prompt and suggest improvements:

Your task is to decide, based on a README file, whether a GitHub repository hosts an engineered software project or not. The possible labels are “true”, “false”, and “unsure”. Provide a reason for the label you assign. Assign the label only based on the content of the README file, do not make assumptions beyond the provided context. An engineered software project is a software project that leverages sound software engineering practices in one or more of its dimensions such as detailed developer documentation, automated testing, or project and issue management. Moreover, the project needs to have a considerable user base and a dedicated purpose. The purpose or goal of the project should be explicitly mentioned in the README file. If the README file is empty or not in English, label the repository as “false”. If the README file indicates that the repository has been archived or moved, is outdated or deprecated, or a mirror or a fork of another repository, label them as “false”. If the README file only contains minimal information or just refers to externally hosted websites or wikis, label them as “unsure”. In the following, we list examples of repositories that should not be considered engineered software projects and should hence be labelled as “false”: - repositories that are collections of code snippets, scripts, examples, configuration files, dot files, or interview questions - repositories hosted for archival purposes - icon, theme, or template repositories - repositories with pre-configured starter projects or auto-generated project scaffolding - repositories that host websites, tutorials, documentation content, or teaching materials - repositories that only host project infrastructure (e.g., pipeline definitions, build scripts, helm charts, etc.) - repositories that host "toy" or hobby projects - projects with a very narrow scope (e.g., single file scripts) - repositories that host datasets or code for AI/ML experiments Return your answer in the following JSON format: { "repo_name": "{repo_name}", "engineered_project": "true" \| "false" \| "unsure", "reason": "..." } Here is the README file of the repository "{repo_name}": {readme_text}

Key Issues & Suggested Improvements

1\. Clarify the decision criteria hierarchy

Right now, some rules overlap or conflict (e.g., “considerable user base” vs. “README-only evidence”). Since the model only sees the README, criteria should be **observable from README content alone**.

**Improve by:**

- Explicitly stating that *signals* of a user base (badges, install instructions, contributors, usage examples) are sufficient.

- Removing any requirement that cannot reasonably be inferred from a README.

2\. Tighten the definition of “engineered software project”

The definition is broad and subjective (“sound software engineering practices”, “considerable user base”).

**Improve by:**

- Replacing abstract wording with **concrete signals** the model can check for.

- Using “one or more strong indicators” instead of vague phrasing.

3\. Make label boundaries sharper

The difference between **false** and **unsure** is currently subtle.

**Improve by:**

- Defining **unsure** as “insufficient evidence despite non-trivial content.”

- Reserving **false** for cases with **explicit disqualifiers** or strong negative evidence.

4\. Add positive examples (optional but recommended)

You list many negative examples but no positive ones.

**Improve by:**

- Adding a short list of examples of repositories that *should* be labeled **true**.

5\. Improve JSON and reasoning guidance

The “reason” field could be more consistent across classifications.

**Improve by:**

- Instructing the model to explicitly reference README evidence.

- Encouraging concise, factual justifications.

Revised Prompt (Improved Version)

Below is a **drop-in replacement** that preserves your intent while improving clarity and robustness:

Task Description

Your task is to decide, **based solely on the content of a GitHub repository’s README file**, whether the repository hosts an **engineered software project**.

You must assign exactly one of the following labels:

- "true"

- "false"

- "unsure"

You must also provide a concise reason explaining your decision, explicitly referring to evidence (or lack thereof) in the README.

Do **not** use external knowledge or assumptions beyond the README text.

Definition: Engineered Software Project

An **engineered software project** is a software system that shows clear evidence of intentional software engineering. This includes **one or more** of the following observable indicators in the README:

- A clearly stated purpose or problem the software solves

- Installation, usage, or deployment instructions

- Developer or contributor documentation

- Evidence of maintenance or collaboration (e.g., contribution guidelines, issue tracking, badges)

- Testing, CI/CD, or quality-related information

- Documentation suggesting real-world or repeated use (e.g., examples, configuration, integrations)

Signals of an active or intended user base (such as usage instructions, badges, community links, or contribution instructions) are sufficient; exact user counts are **not required**.

Labeling Rules

Assign "false" if any of the following apply:

- The README is empty or not written in English

- The repository is explicitly described as:

  - archived, deprecated, outdated, or unmaintained

  - moved to another repository

  - a mirror or fork with no independent development

- The repository clearly falls into one of the following categories:

  - collections of code snippets, scripts, examples, dotfiles, or interview questions

  - archival repositories

  - icon, theme, or template repositories

  - auto-generated project scaffolding or starter kits

  - websites, tutorials, documentation-only, or teaching materials

  - repositories hosting only infrastructure (e.g., CI configs, Helm charts)

  - toy, hobby, or experimental projects

  - projects with a very narrow scope (e.g., a single-file script)

  - datasets or code solely for AI/ML experiments

Assign "unsure" if:

- The README contains some information but **insufficient evidence** to determine whether the project is engineered

- The README is very minimal, vague, or mostly redirects to external websites or wikis without describing the project itself

Assign "true" if:

- The README clearly describes a software project with a defined purpose **and  
  **

- There is reasonable evidence of software engineering practices or intended real-world use

Output Format

Return your answer in the following JSON format:

{

"repo_name": "{repo_name}",

"engineered_project": "true" \| "false" \| "unsure",

"reason": "Brief explanation citing specific README evidence or explaining what is missing."

}

Input

Here is the README file of the repository **"{repo_name}"**:

{readme_text}

Optional Enhancements (If You Want Even More Consistency)

- Add a **decision checklist** (e.g., “Purpose stated?”, “Usage described?”).

- Add 2–3 **example classifications** (one per label).

- Impose a **length limit** on the reason (e.g., max 2 sentences).

If you’d like, I can also:

- Convert this into a **few-shot prompt  
  **

- Optimize it for **LLM-based batch classification  
  **

- Help define **inter-annotator agreement rules** for human validation

[<u>https://chatgpt.com/share/696def05-4870-8005-b227-30335c59b8f8</u>](https://chatgpt.com/share/696def05-4870-8005-b227-30335c59b8f8)

Check the revised version: Your task is to decide, based solely on the content of a GitHub repository’s README file, whether the repository hosts an engineered software project. You must assign exactly one of the following labels: \* "true" \* "false" \* "unsure" You must also provide a concise justification explaining your decision, explicitly referring to evidence (or lack thereof) in the README. Do not use external knowledge or assumptions beyond the README text. \## Definition: Engineered Software Project An engineered software project is a software project that shows clear evidence of following established software engineering practices. This includes one or more of the following observable signals in the README, but there can be more: \* A clearly stated purpose or problem the software solves. \* Installation, usage, or deployment instructions. \* Developer or contributor documentation. \* Evidence of maintenance (e.g., contribution guidelines, issue tracking). \* Testing, CI/CD, or quality-assurance-related information. \* User documentation suggesting real-world or repeated use (e.g., examples, configuration, integrations). \## Labeling Rules Assign "false" if any of the following apply: \* The README is empty or not written in English \* The repository is explicitly described as archived, deprecated, outdated, or unmaintained; moved to another repository; or a mirror or fork of another repository. \* The repository falls into one of the following categories: \* collections of code snippets, scripts, examples, dotfiles, or interview questions \* archival repositories \* icon, theme, or template repositories \* auto-generated project scaffolding or starter kits \* websites, tutorials, documentation-only, or teaching materials \* repositories hosting only infrastructure (e.g., CI configs, Helm charts) \* toy, hobby, or experimental projects \* projects with a very narrow scope (e.g., a single-file script) \* datasets or code for AI/ML experiments Assign "unsure" if: \* The README is very minimal, vague, or mostly redirects to external websites or wikis without describing the project itself. \* The README contains some information but insufficient evidence to determine whether the project is engineered or not. Assign "true" if: \* The README clearly describes a software project with a defined purpose and there is reasonable evidence of software engineering practices and intended real-world use. \## Output Format Return your answer in the following JSON format: { "repo_name": "{repo_name}", "engineered_project": "true" \| "false" \| "unsure", "justification": "Brief explanation citing specific README evidence or explaining what is missing." } \## Input Here is the README file of the repository "{repo_name}": {readme_text}

Your revised version is **strong and clear**—it’s a clear improvement over the earlier draft. Below is a focused review with **minor refinements** to further tighten precision, reduce ambiguity, and improve annotation consistency. I’ll separate this into **what works well** and **suggested improvements**, followed by a **lightly polished version**.

## **What Works Well**

✅ **Clear task framing  
** The scope (“based solely on the README”) and prohibition of external assumptions are explicit and effective.

✅ **Operationalized definition  
** The list of “observable signals” is concrete and well-aligned with what a README can reasonably show.

✅ **Well-separated labels  
** The distinction between false, unsure, and true is much clearer than before.

✅ **Consistent terminology  
** You consistently use “engineered software project” and “README,” which reduces ambiguity.

✅ **JSON output specification  
** The schema is explicit and unambiguous—excellent for automated evaluation.

## **Suggested Improvements (Minor but Valuable)**

### **1. Make “false” vs. “unsure” mutually exclusive by intent**

Right now, *minimal README* appears only under unsure, but minimal READMEs could sometimes overlap with categories listed under false.

**Suggestion:  
** Explicitly state that false applies when there is **clear negative or disqualifying evidence**, while unsure applies when evidence is **insufficient or ambiguous**.

### **2. Slightly soften the AI/ML experiments rule**

“datasets or code for AI/ML experiments” can unintentionally exclude well-engineered ML frameworks.

**Suggestion:  
** Clarify that this applies to *experimental or research-only* repositories without engineering signals.

### **3. Add a short instruction on justification style**

This improves consistency across annotations.

**Suggestion:  
** Ask for 1–2 sentences and require citing *specific README elements*.

### **4. Minor wording polish**

A few sentences can be made more concise or parallel without changing meaning.

## **Polished Version (Minimal Changes)**

Below is your text with **only light edits**, preserving your structure and intent:

Your task is to decide, **based solely on the content of a GitHub repository’s README file**, whether the repository hosts an engineered software project.

You must assign exactly one of the following labels:

- "true"

- "false"

- "unsure"

You must also provide a concise justification explaining your decision, explicitly referring to evidence (or lack thereof) in the README.  
Do **not** use external knowledge or assumptions beyond the README text.

## **Definition: Engineered Software Project**

An engineered software project is a software project that shows clear evidence of following established software engineering practices. This includes one or more of the following observable signals in the README (this list is non-exhaustive):

- A clearly stated purpose or problem the software solves

- Installation, usage, or deployment instructions

- Developer or contributor documentation

- Evidence of maintenance (e.g., contribution guidelines, issue tracking)

- Testing, CI/CD, or quality-assurance-related information

- User documentation suggesting real-world or repeated use (e.g., examples, configuration, integrations)

## **Labeling Rules**

### **Assign "false" if any of the following apply:**

- The README is empty or not written in English

- The repository is explicitly described as archived, deprecated, outdated, or unmaintained; moved to another repository; or a mirror or fork of another repository with no independent development

- The repository clearly falls into one of the following categories:

  - collections of code snippets, scripts, examples, dotfiles, or interview questions

  - archival repositories

  - icon, theme, or template repositories

  - auto-generated project scaffolding or starter kits

  - websites, tutorials, documentation-only, or teaching materials

  - repositories hosting only infrastructure (e.g., CI configs, Helm charts)

  - toy, hobby, or purely experimental projects

  - projects with a very narrow scope (e.g., a single-file script)

  - datasets or code for AI/ML experiments **without evidence of software engineering or production use  
    **

### **Assign "unsure" if:**

- The README is very minimal, vague, or mostly redirects to external websites or wikis without describing the project itself

- The README contains some information, but there is insufficient or ambiguous evidence to determine whether the project is engineered

### **Assign "true" if:**

- The README clearly describes a software project with a defined purpose **and  
  **

- There is reasonable evidence of software engineering practices and intended real-world use

## **Output Format**

Return your answer in the following JSON format:

{

"repo_name": "{repo_name}",

"engineered_project": "true" \| "false" \| "unsure",

"justification": "One to two sentences citing specific README evidence or explaining what is missing."

}

## **Input**

Here is the README file of the repository **"{repo_name}"**:

{readme_text}

## **Overall Verdict**

⭐ **This prompt is well-designed and ready for use  
** With the small refinements above, it should yield:

- Higher inter-annotator agreement

- More consistent LLM outputs

- Fewer borderline misclassifications

If you want, I can also:

- Stress-test it against tricky real-world READMEs

- Convert it into a scoring-based rubric

- Create a short annotator guideline document for humans

# AIware Paper Version

The following prompt was used for the AIware paper. It classifies repositories based solely on the README text, without supplementary context (linguist data, link summaries).

Your task is to decide, based solely on the content of a GitHub repository’s README file, whether the repository hosts an engineered software project.

You must assign exactly one of the following labels:

\* "true"

\* "false"

\* "unsure"

You must also provide a concise justification explaining your decision, explicitly referring to evidence (or lack thereof) in the README.

Do not use external knowledge or assumptions beyond the README text.

\## Definition: Engineered Software Project

An engineered software project is a software project that shows clear evidence of following established software engineering practices. This includes one or more of the following observable signals in the README (this list is non-exhaustive):

\* A clearly stated purpose or problem the software solves.

\* Installation, usage, or deployment instructions.

\* Developer or contributor documentation.

\* Evidence of maintenance (e.g., contribution guidelines, issue tracking).

\* Testing, CI/CD, or quality-assurance-related information.

\* User documentation suggesting real-world or repeated use (e.g., examples, configuration, integrations).

\## Labeling Rules

Assign "false" if any of the following apply:

\* The README is empty or not written in English.

\* The repository is explicitly described as archived, deprecated, outdated, or unmaintained; moved to another repository; or a mirror or fork of another repository.

\* The repository falls into one of the following categories:

\* Collections of code snippets, scripts, examples, dotfiles, or interview questions.

\* Archival repositories.

\* Icon, theme, or template repositories.

\* Auto-generated project scaffolding or starter kits.

\* Websites, tutorials, documentation-only, or teaching materials.

\* Repositories hosting only infrastructure (e.g., CI configs, Helm charts).

\* Toy, hobby, or experimental projects.

\* Projects with a very narrow scope (e.g., a single-file script).

\* Datasets or code for AI/ML experiments.

Assign "unsure" if:

\* The README is very minimal, vague, or mostly redirects to external websites or wikis without describing the project itself.

\* The README contains some information but insufficient evidence to determine whether the project is engineered or not.

Assign "true" if:

\* The README clearly describes a software project with a defined purpose and there is reasonable evidence of software engineering practices and intended real-world use.

\## Output Format

Return your answer in the following JSON format:

{

"repo_name": "{repo_name}",

"engineered_project": "true" \| "false" \| "unsure",

"justification": "One to two sentences citing specific README evidence or explaining what is missing."

}

\## Input

Here is the README file of the repository "{repo_name}":

{readme_text}
