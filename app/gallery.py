"""Face gallery (stub).

Stores enrolled people's embeddings and matches new faces against them.
Responsibilities, to be built in the prototype phase:

- Build the gallery from ``known/<person>/*.jpg`` (5 to 10 photos each),
  rejecting photos with zero or more than one face.
- Keep every embedding per person; save to ``data/gallery.npz``
  (``app.config.GALLERY_PATH``). This file is biometric data and is
  git-ignored.
- Match by cosine similarity: take each person's best score, accept the
  top person only if it clears ``MATCH_THRESHOLD`` and beats the
  runner-up by ``MATCH_MARGIN``; otherwise the face is unknown.
- Later: move storage to SQLite, then PostgreSQL (``face_embeddings`` table).
"""
