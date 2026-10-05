package com.titanarq.studentassistant.ui

import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.protocol.User
import com.titanarq.studentassistant.users.UserAvatar
import com.titanarq.studentassistant.users.UserPhotos

/**
 * The one top bar of the app: [onBack] (null on the initial screen, where there is nothing to go
 * back to) draws the Back icon at the left, the [title] follows it immediately, and [actions] sit
 * at the right. Every screen is drawn through [AppScaffold], so they all look the same.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppTopBar(
    title: String,
    onBack: (() -> Unit)?,
    modifier: Modifier = Modifier,
    actions: @Composable RowScope.() -> Unit = {},
) {
    TopAppBar(
        title = { Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
        modifier = modifier,
        navigationIcon = {
            if (onBack != null) {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = stringResource(R.string.back))
                }
            }
        },
        actions = actions,
        colors = TopAppBarDefaults.topAppBarColors(
            // The web's header band (`--header-bg` / `--on-header`), through the theme's primaryContainer.
            containerColor = MaterialTheme.colorScheme.primaryContainer,
            titleContentColor = MaterialTheme.colorScheme.onPrimaryContainer,
            navigationIconContentColor = MaterialTheme.colorScheme.onPrimaryContainer,
            actionIconContentColor = MaterialTheme.colorScheme.onPrimaryContainer,
        ),
    )
}

/** The gear that opens the computers/settings screen. */
@Composable
fun SettingsAction(onClick: () -> Unit) {
    IconButton(onClick = onClick) {
        Icon(Icons.Filled.Settings, contentDescription = stringResource(R.string.settings_description))
    }
}

/** The right side of the bar once a user is chosen: the gear, then the user's avatar (opens «Editar perfil»). */
@Composable
fun RowScope.SessionActions(user: User?, photos: UserPhotos, onSettings: () -> Unit, onProfile: () -> Unit) {
    SettingsAction(onSettings)
    if (user != null) {
        IconButton(onClick = onProfile) {
            UserAvatar(
                user,
                photos,
                size = AVATAR_SIZE,
                contentDescription = stringResource(R.string.profile_description, user.name),
            )
        }
    }
}

/** A screen with the shared [AppTopBar] above [content]; the content gets the bar's padding. */
@Composable
fun AppScaffold(
    title: String,
    onBack: (() -> Unit)?,
    modifier: Modifier = Modifier,
    actions: @Composable RowScope.() -> Unit = {},
    content: @Composable (PaddingValues) -> Unit,
) {
    Scaffold(
        modifier = modifier.fillMaxSize(),
        topBar = { AppTopBar(title, onBack, actions = actions) },
        containerColor = MaterialTheme.colorScheme.background,
    ) { padding -> content(padding) }
}

private val AVATAR_SIZE = 32.dp
