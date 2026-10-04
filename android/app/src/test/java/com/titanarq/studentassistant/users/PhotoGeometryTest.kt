package com.titanarq.studentassistant.users

import org.junit.Assert.assertEquals
import org.junit.Test

class PhotoGeometryTest {
    @Test
    fun `a small image is neither enlarged nor shrunk`() {
        assertEquals(PhotoSize(800, 600), photoTargetSize(800, 600, 1))
    }

    @Test
    fun `a large landscape image has its long edge at 1024`() {
        assertEquals(PhotoSize(1024, 768), photoTargetSize(4000, 3000, 1))
    }

    @Test
    fun `a quarter turn swaps the sides before scaling`() {
        // A portrait photo stored as landscape pixels with orientation 6 (rotate 90).
        assertEquals(PhotoSize(768, 1024), photoTargetSize(4000, 3000, 6))
        assertEquals(PhotoSize(600, 800), photoTargetSize(800, 600, 8))
        assertEquals(PhotoSize(600, 800), photoTargetSize(800, 600, 5))
        assertEquals(PhotoSize(600, 800), photoTargetSize(800, 600, 7))
    }

    @Test
    fun `half turns and mirrors keep the sides`() {
        for (orientation in listOf(2, 3, 4)) {
            assertEquals(PhotoSize(800, 600), photoTargetSize(800, 600, orientation))
        }
    }

    @Test
    fun `an unknown orientation is as is`() {
        assertEquals(PhotoSize(800, 600), photoTargetSize(800, 600, 0))
        assertEquals(PhotoSize(800, 600), photoTargetSize(800, 600, 99))
    }

    @Test
    fun `a very thin image keeps at least one pixel`() {
        assertEquals(PhotoSize(1024, 1), photoTargetSize(10000, 1, 1))
    }

    @Test
    fun `the orientation tag maps to rotation and mirror`() {
        assertEquals(ExifTransform(0, false), exifTransform(1))
        assertEquals(ExifTransform(0, true), exifTransform(2))
        assertEquals(ExifTransform(180, false), exifTransform(3))
        assertEquals(ExifTransform(180, true), exifTransform(4))
        assertEquals(ExifTransform(90, true), exifTransform(5))
        assertEquals(ExifTransform(90, false), exifTransform(6))
        assertEquals(ExifTransform(270, true), exifTransform(7))
        assertEquals(ExifTransform(270, false), exifTransform(8))
    }

    @Test
    fun `the sample size keeps both sides at or above the target`() {
        assertEquals(1, photoSampleSize(1500, 1500))
        assertEquals(2, photoSampleSize(4000, 3000))
        assertEquals(4, photoSampleSize(8000, 6000))
        assertEquals(1, photoSampleSize(8000, 1000))
    }
}
