package com.titanarq.studentassistant.home

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle
import java.util.Locale

/** The day of a topic's last capture, as the topic card shows it (the date only, no time). */
fun formatCaptureDate(epochMs: Long, locale: Locale, zone: ZoneId): String =
    DateTimeFormatter.ofLocalizedDate(FormatStyle.MEDIUM).withLocale(locale)
        .format(Instant.ofEpochMilli(epochMs).atZone(zone))
