package com.titanarq.studentassistant.users

/** The longest edge of the photo sent to the backend, in pixels (#555). */
const val PHOTO_MAX_EDGE_PX: Int = 1024

/** JPEG quality of the photo sent to the backend. */
const val PHOTO_JPEG_QUALITY: Int = 85

/** The most a photo may weigh when sent (the backend's usual 5 MiB cap). */
const val PHOTO_MAX_BYTES: Int = 5 * 1024 * 1024

/** A size in pixels. */
data class PhotoSize(val width: Int, val height: Int)

/** What to do to the decoded pixels to show them upright: rotate clockwise, then mirror left-right. */
data class ExifTransform(val rotationDegrees: Int, val flipHorizontal: Boolean)

/** The [ExifTransform] of an EXIF orientation tag (1..8; anything else is "as is"). */
fun exifTransform(exifOrientation: Int): ExifTransform = when (exifOrientation) {
    2 -> ExifTransform(0, true)
    3 -> ExifTransform(180, false)
    4 -> ExifTransform(180, true)
    5 -> ExifTransform(90, true)
    6 -> ExifTransform(90, false)
    7 -> ExifTransform(270, true)
    8 -> ExifTransform(270, false)
    else -> ExifTransform(0, false)
}

/**
 * The size of the upright photo to send: [width] x [height] as decoded, turned by
 * [exifOrientation] (a quarter turn swaps them) and shrunk so the long edge is at most
 * [PHOTO_MAX_EDGE_PX]; a smaller image is never enlarged.
 */
fun photoTargetSize(width: Int, height: Int, exifOrientation: Int): PhotoSize {
    require(width > 0 && height > 0) { "image of ${width}x$height" }
    val turned = exifTransform(exifOrientation).rotationDegrees % 180 != 0
    val w = if (turned) height else width
    val h = if (turned) width else height
    val longEdge = maxOf(w, h)
    if (longEdge <= PHOTO_MAX_EDGE_PX) return PhotoSize(w, h)
    val scale = PHOTO_MAX_EDGE_PX.toDouble() / longEdge
    return PhotoSize(
        maxOf(1, Math.round(w * scale).toInt()),
        maxOf(1, Math.round(h * scale).toInt()),
    )
}

/**
 * The power-of-two `inSampleSize` that decodes [width] x [height] no smaller than the target, so a
 * 12 MP photo is never fully decoded just to be shrunk.
 */
fun photoSampleSize(width: Int, height: Int): Int {
    var sample = 1
    while (width / (sample * 2) >= PHOTO_MAX_EDGE_PX && height / (sample * 2) >= PHOTO_MAX_EDGE_PX) sample *= 2
    return sample
}
