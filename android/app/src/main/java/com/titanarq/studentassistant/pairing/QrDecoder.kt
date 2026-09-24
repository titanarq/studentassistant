package com.titanarq.studentassistant.pairing

import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.NotFoundException
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.ReaderException
import com.google.zxing.common.HybridBinarizer

/**
 * Finds a QR code in one grayscale camera frame with ZXing core (no Google Play Services).
 * Plain JVM code: the CameraX analyzer hands it the frame's Y (luminance) plane.
 */
class QrDecoder {
    private val reader = MultiFormatReader().apply {
        setHints(
            mapOf(
                DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
                DecodeHintType.TRY_HARDER to true,
            ),
        )
    }

    /**
     * Decodes the QR in a luminance plane of [width] x [height] pixels whose rows are [rowStride]
     * bytes apart (pixel stride 1, as CameraX's YUV_420_888 Y plane). Null when there is none.
     */
    fun decode(luminance: ByteArray, width: Int, height: Int, rowStride: Int = width): String? {
        val source = PlanarYUVLuminanceSource(luminance, rowStride, height, 0, 0, width, height, false)
        return try {
            reader.decodeWithState(BinaryBitmap(HybridBinarizer(source))).text
        } catch (e: NotFoundException) {
            null
        } catch (e: ReaderException) {
            null
        } finally {
            reader.reset()
        }
    }
}
