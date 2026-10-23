const MAX_SIDE = 1600;

/** A picked file as a JPEG data URL, shrunk so ten phone photos fit in one request. Rejects if it isn't an image. */
export type ImagePreparer = (file: File) => Promise<string>;

export async function shrinkImage(file: File): Promise<string> {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch (e) {
    throw new Error("Not an image the browser can read", { cause: e });
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d")?.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return canvas.toDataURL("image/jpeg", 0.92);
}
