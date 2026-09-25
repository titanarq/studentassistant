package com.titanarq.studentassistant.desk

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.webkit.URLUtil
import android.widget.Toast
import com.titanarq.studentassistant.R

/**
 * Hands a download the study desk page started (#259) to the system [DownloadManager], with a
 * visible notification, or tells the student why it did not start. The decision is
 * [decideDownload]'s; the token only travels in the request header and is never logged.
 */
internal fun startDeskDownload(
    context: Context,
    url: String,
    contentDisposition: String?,
    mimeType: String?,
    page: DeskUiState.Ready,
) {
    val decision = decideDownload(url, contentDisposition, mimeType, page.baseUrl, page.page.token) { u, cd, mt ->
        URLUtil.guessFileName(u, cd, mt)
    }
    val message = when (decision) {
        is DownloadDecision.Refused -> context.getString(R.string.desk_download_refused)
        is DownloadDecision.Start -> if (enqueue(context, decision.download)) {
            context.getString(R.string.desk_download_started, decision.download.fileName)
        } else {
            context.getString(R.string.desk_download_failed)
        }
    }
    Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
}

private fun enqueue(context: Context, download: DeskDownload): Boolean {
    val manager = context.getSystemService(DownloadManager::class.java) ?: return false
    return try {
        val request = DownloadManager.Request(Uri.parse(download.url)).apply {
            download.headers.forEach { (name, value) -> addRequestHeader(name, value) }
            download.mimeType?.let { setMimeType(it) }
            setTitle(download.fileName)
            setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                // The shared Downloads folder needs no storage permission from Android 10 on.
                setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, download.fileName)
            } else {
                // Android 9 would need WRITE_EXTERNAL_STORAGE for it: keep the file in the app's
                // own external folder, still opened from the download notification.
                setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, download.fileName)
            }
        }
        manager.enqueue(request)
        true
    } catch (_: IllegalArgumentException) {
        false
    } catch (_: IllegalStateException) {
        false
    } catch (_: SecurityException) {
        false
    }
}
