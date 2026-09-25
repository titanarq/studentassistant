package com.titanarq.studentassistant.protocol

/**
 * `protocol_version` (MAJOR.MINOR) and its negotiation, as `protocol/README.md` defines it.
 *
 * Peers with the same MAJOR interoperate and speak the lower of the two MINORs; a different MAJOR
 * is refused with a message naming both versions.
 */
const val PROTOCOL_VERSION: String = "1.6"

private val VERSION_REGEX = Regex("^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$")

/** A parsed `MAJOR.MINOR` protocol version, ordered by MAJOR then MINOR. */
data class ProtocolVersion(val major: Int, val minor: Int) : Comparable<ProtocolVersion> {
    override fun compareTo(other: ProtocolVersion): Int =
        compareValuesBy(this, other, ProtocolVersion::major, ProtocolVersion::minor)

    override fun toString(): String = "$major.$minor"
}

/** The peer speaks a protocol MAJOR version this side does not. */
class IncompatibleProtocolVersionException(
    val peer: String,
    val ours: String = PROTOCOL_VERSION,
) : IllegalArgumentException(
    "incompatible protocol_version $peer: this side speaks $ours; " +
        "update the older side so both share MAJOR version ${parseVersion(ours).major}",
)

/** Splits `MAJOR.MINOR` into integers; anything else is an [IllegalArgumentException]. */
fun parseVersion(version: String): ProtocolVersion {
    val match = VERSION_REGEX.matchEntire(version)
        ?: throw IllegalArgumentException("malformed protocol_version '$version': expected MAJOR.MINOR")
    return ProtocolVersion(match.groupValues[1].toInt(), match.groupValues[2].toInt())
}

/** Whether [peer] shares our MAJOR version (a malformed [peer] is an [IllegalArgumentException]). */
fun isCompatible(peer: String, ours: String = PROTOCOL_VERSION): Boolean =
    parseVersion(peer).major == parseVersion(ours).major

/** Throws [IncompatibleProtocolVersionException] unless [peer] shares our MAJOR version. */
fun checkCompatible(peer: String, ours: String = PROTOCOL_VERSION) {
    if (!isCompatible(peer, ours)) throw IncompatibleProtocolVersionException(peer, ours)
}

/** The version both sides speak: the shared MAJOR with the lower MINOR. */
fun negotiate(peer: String, ours: String = PROTOCOL_VERSION): String {
    checkCompatible(peer, ours)
    return minOf(parseVersion(peer), parseVersion(ours)).toString()
}
