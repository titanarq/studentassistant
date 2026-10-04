package com.titanarq.studentassistant.ui

import com.titanarq.studentassistant.backend.PairedBackend
import com.titanarq.studentassistant.backend.PairedBackends
import com.titanarq.studentassistant.protocol.User
import org.junit.Assert.assertEquals
import org.junit.Test

class RouteTest {
    private val home = PairedBackend("http://pc:8000", "d1", "sa_1", "pc:8000")
    private val stored = PairedBackends(listOf(home), "d1")
    private val laura = User("laura", "Laura")

    @Test
    fun `pairing comes first when no backend is stored`() {
        assertEquals(Route.PAIRING, startRoute(PairedBackends(), null))
        assertEquals(Route.PAIRING, startRoute(PairedBackends(), laura))
    }

    @Test
    fun `at every process start a stored backend opens the user selection`() {
        assertEquals(Route.USERS, startRoute(stored, null))
    }

    @Test
    fun `with a user selected the app opens the home`() {
        assertEquals(Route.HOME, startRoute(stored, laura))
    }

    @Test
    fun `after sign-out or a backend switch the screens that need a user ask who is using the app`() {
        for (route in listOf(Route.HOME, Route.CAPTURE, Route.PROFILE)) {
            assertEquals(Route.USERS, routeFor(route, hasBackends = true, user = null))
            assertEquals(route, routeFor(route, hasBackends = true, user = laura))
        }
    }

    @Test
    fun `pairing, the backends list and the user selection itself never need a user`() {
        for (route in listOf(Route.PAIRING, Route.BACKENDS, Route.CONNECTION_TEST, Route.USERS)) {
            assertEquals(route, routeFor(route, hasBackends = true, user = null))
        }
        assertEquals(Route.HOME, routeFor(Route.HOME, hasBackends = false, user = null))
    }

    @Test
    fun `the initial screens have no back, pairing goes back to the computers only when some are paired`() {
        assertEquals(null, backRoute(Route.USERS, hasBackends = true, settingsFrom = Route.HOME))
        assertEquals(null, backRoute(Route.PAIRING, hasBackends = false, settingsFrom = Route.HOME))
        assertEquals(Route.BACKENDS, backRoute(Route.PAIRING, hasBackends = true, settingsFrom = Route.HOME))
    }

    @Test
    fun `settings goes back to the screen whose gear opened it`() {
        assertEquals(Route.USERS, backRoute(Route.BACKENDS, hasBackends = true, settingsFrom = Route.USERS))
        assertEquals(Route.HOME, backRoute(Route.BACKENDS, hasBackends = true, settingsFrom = Route.HOME))
        assertEquals(Route.BACKENDS, backRoute(Route.CONNECTION_TEST, hasBackends = true, settingsFrom = Route.HOME))
        assertEquals(Route.HOME, backRoute(Route.PROFILE, hasBackends = true, settingsFrom = Route.USERS))
    }
}
