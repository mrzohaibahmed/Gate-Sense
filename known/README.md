# known/: enrollment photos

One subfolder per enrolled person. The folder name is the person's employee code, which is also their ID in the gallery and the `persons` table. The dashboard's Enrollment page creates and fills these folders for you.

```
known/
  waqas/
    1.jpg
    2.jpg
    ...
  e042/
    1.jpg
    ...
```

- Employee codes start with a letter or digit and use only letters, digits, `_`, `-` and `.`.
- Use 5 to 10 photos per person: straight on, slightly left and right, with and without glasses.
- Each photo must contain exactly one face, at least 80 px wide and in sharp focus.
- Photos taken with the actual gate camera give the best accuracy, because the lighting and angle match.
- Only enroll people who have signed consent.

**These photos are biometric data.** Everything in this folder except this README is git-ignored and must never be committed.
