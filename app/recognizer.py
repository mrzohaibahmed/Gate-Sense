"""Face recognition (stub).

Turns a detected face into a 512-d ArcFace embedding using the buffalo_s
recognition model (MobileFaceNet) on CPU. Responsibilities, to be built in
the prototype phase:

- Align the face to 112x112 using its 5 landmarks.
- Compute the embedding and L2-normalize it so cosine similarity is a
  plain dot product.
- No training: embeddings come straight from the pretrained model.
"""
