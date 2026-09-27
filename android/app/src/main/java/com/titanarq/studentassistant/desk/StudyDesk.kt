package com.titanarq.studentassistant.desk

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * The web UI's study desk on the phone (#83): the backend serves the web app, and the app shows one
 * of a topic's two screens in a WebView (#414): **Construir** (the workspace, `/workspace`: the
 * document, its sources and the chat that drives the work) or **Estudiar** (`/study`: the study
 * modes and the material generated from the notes), the same two entries the web desk offers. The page
 * cannot send `Authorization: Bearer`, so the paired token travels in the [TOKEN_COOKIE] cookie,
 * which the backend accepts in its place (docs/modules/server.md).
 */

/** The cookie the backend reads a paired device's token from when no bearer header comes. */
const val TOKEN_COOKIE = "sa_token"

/** Which of a topic's two web screens the study desk shows; [segment] is its last URL path segment. */
enum class DeskView(val segment: String) {
    /** «Construir»: the topic's workspace. */
    WORKSPACE("workspace"),

    /** «Estudiar»: the topic's study screen. */
    STUDY("study"),
}

/** The topic, and which of its screens ([view]), the study desk screen shows. */
data class DeskTopic(
    val subjectId: String,
    val topicId: String,
    val topicName: String,
    val view: DeskView = DeskView.WORKSPACE,
)

/**
 * What the WebView loads: [url], after setting [cookie] for [cookieUrl] (the backend's origin).
 * [token] is the paired token itself, sent as `Authorization: Bearer` on the downloads the page
 * hands to the system (#259). [cookie] and [token] never appear in [toString].
 */
data class DeskPage(val url: String, val cookieUrl: String, val cookie: String, val token: String) {
    override fun toString(): String = "DeskPage(url=$url, cookieUrl=$cookieUrl, cookie=<redacted>, token=<redacted>)"
}

/**
 * The web page [view] of a topic on the backend at [baseUrl]:
 * `<baseUrl>/subjects/<subjectId>/topics/<topicId>/<view.segment>`, each id percent-encoded as one
 * path segment, the base URL's path and query dropped. Null when [baseUrl] is not an `http(s)` URL.
 */
fun topicPageUrl(baseUrl: String, subjectId: String, topicId: String, view: DeskView): String? {
    val base = baseUrl.toHttpUrlOrNull() ?: return null
    return base.newBuilder()
        .encodedPath("/")
        .addPathSegment("subjects")
        .addPathSegment(subjectId)
        .addPathSegment("topics")
        .addPathSegment(topicId)
        .addPathSegment(view.segment)
        .query(null)
        .fragment(null)
        .build()
        .toString()
}

/** «Construir»: `<baseUrl>/subjects/<s>/topics/<t>/workspace`; null for a bad base URL. */
fun workspacePageUrl(baseUrl: String, subjectId: String, topicId: String): String? =
    topicPageUrl(baseUrl, subjectId, topicId, DeskView.WORKSPACE)

/** «Estudiar»: `<baseUrl>/subjects/<s>/topics/<t>/study`; null for a bad base URL. */
fun studyPageUrl(baseUrl: String, subjectId: String, topicId: String): String? =
    topicPageUrl(baseUrl, subjectId, topicId, DeskView.STUDY)

/** The backend's origin (`scheme://host:port/`), the URL its cookie is set for; null when not http(s). */
fun backendOrigin(baseUrl: String): String? = baseUrl.toHttpUrlOrNull()?.let { origin(it) }

/**
 * The `Set-Cookie` string that hands [token] to the backend: sent only to its origin's paths,
 * hidden from the page's scripts, never on a request another site starts. No `Secure`: the backend
 * serves plain HTTP on the LAN.
 */
fun tokenCookie(token: String): String = "$TOKEN_COOKIE=$token; Path=/; HttpOnly; SameSite=Strict"

/** The page [DeskTopic.view] of [topic] on the backend at [baseUrl] with [token]; null for a bad base URL. */
fun deskPage(baseUrl: String, token: String, topic: DeskTopic): DeskPage? {
    val url = topicPageUrl(baseUrl, topic.subjectId, topic.topicId, topic.view) ?: return null
    val cookieUrl = backendOrigin(baseUrl) ?: return null
    return DeskPage(url, cookieUrl, tokenCookie(token), token)
}

/**
 * Whether [url] is on the same origin (scheme, host and port) as [baseUrl]: the WebView stays
 * there, and every other link opens in the system browser.
 */
fun isSameOrigin(url: String, baseUrl: String): Boolean {
    val target = url.toHttpUrlOrNull() ?: return false
    val base = baseUrl.toHttpUrlOrNull() ?: return false
    return origin(target) == origin(base)
}

private fun origin(url: HttpUrl): String =
    HttpUrl.Builder().scheme(url.scheme).host(url.host).port(url.port).build().toString()
