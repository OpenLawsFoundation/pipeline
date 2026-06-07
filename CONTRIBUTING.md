# Contributing

Thanks for helping build the Open Laws Foundation. This document covers how to
contribute and, importantly, the **sign-off** we require on every commit.

## License of contributions

This repository is licensed under **Apache License 2.0**. By contributing, you
agree that your contributions are licensed under the same terms, including the
patent grant in section 3 of that license.

(The [`archive`](https://github.com/OpenLawsFoundation/archive) repository is
different — it is CC0-1.0, because it holds legal texts and their markup, not
code. This file applies to the code repositories: `spec`, `pipeline`, `diff`.)

## Developer Certificate of Origin (DCO)

We use the **DCO** instead of a heavyweight CLA. It is a lightweight, per-commit
statement that you have the right to submit your contribution. This keeps every
contribution cleanly attributable and transferable — which matters, because when
the project incorporates a non-profit entity, the code must move to that entity
without chasing down every past contributor.

**How to sign off:** add a `Signed-off-by` line to every commit. Git does this
for you with the `-s` flag:

```
git commit -s -m "Add Légifrance lifecycle mapping"
```

This appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

Use your real name and a real email. By signing off you certify the DCO below.
A bot checks that every commit in a pull request is signed off.

> Tip: set `git config --global user.name` and `user.email` once, and consider a
> commit template or `git config alias.cs 'commit -s'` so you never forget.

---

## How to contribute

- **A new jurisdiction** → open an issue in `pipeline` first, then submit an
  adapter. It must emit native Akoma Ntoso + the AKN4OLF metadata layer and pass
  the conformance suite in `spec`.
- **The standard (AKN4OLF) or the conformance suite** → discuss in an issue in
  `spec` before a PR; changes here ripple across every adapter.
- **The differ** → PRs to `diff`. Include test fixtures (before/after Akoma Ntoso
  snippets) for any new changeset type.
- **Bug fixes, docs, tooling** → PRs welcome directly.

Open an issue before any structural change to on-disk formats or the spec.

---

## Developer Certificate of Origin 1.1

By making a contribution to this project, I certify that:

> (a) The contribution was created in whole or in part by me and I have the right
> to submit it under the open source license indicated in the file; or
>
> (b) The contribution is based upon previous work that, to the best of my
> knowledge, is covered under an appropriate open source license and I have the
> right under that license to submit that work with modifications, whether
> created in whole or in part by me, under the same open source license (unless I
> am permitted to submit under a different license), as indicated in the file; or
>
> (c) The contribution was provided directly to me by some other person who
> certified (a), (b) or (c) and I have not modified it.
>
> (d) I understand and agree that this project and the contribution are public and
> that a record of the contribution (including all personal information I submit
> with it, including my sign-off) is maintained indefinitely and may be
> redistributed consistent with this project or the open source license(s)
> involved.

*The DCO text above is the Developer Certificate of Origin, Version 1.1
(https://developercertificate.org/), reproduced verbatim.*
