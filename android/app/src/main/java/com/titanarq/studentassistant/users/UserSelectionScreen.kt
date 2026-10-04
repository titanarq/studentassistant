package com.titanarq.studentassistant.users

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.ui.backendFailureMessage

/** «¿Quién eres?»: the users of the active backend; tapping one makes every call act for them. */
@Composable
fun UserSelectionScreen(
    viewModel: UsersViewModel,
    photos: UserPhotos,
    onSelected: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { viewModel.load() }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            when (val current = state) {
                UsersUiState.Loading -> Text(stringResource(R.string.loading))
                UsersUiState.NoBackend -> Text(stringResource(R.string.users_no_backend))
                is UsersUiState.Failed -> Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(backendFailureMessage(current.failure), color = MaterialTheme.colorScheme.error)
                    OutlinedButton(onClick = viewModel::load) { Text(stringResource(R.string.home_retry)) }
                }
                is UsersUiState.Loaded ->
                    if (current.users.isEmpty()) {
                        Text(stringResource(R.string.users_empty))
                    } else {
                        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            items(current.users, key = { it.id }) { user ->
                                Card(
                                    modifier = Modifier.fillMaxWidth().clickable {
                                        viewModel.select(user)
                                        onSelected()
                                    },
                                ) {
                                    Row(
                                        modifier = Modifier.padding(12.dp),
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                                    ) {
                                        UserAvatar(user, photos)
                                        Column {
                                            Text(user.name, style = MaterialTheme.typography.titleMedium)
                                            user.email?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                                        }
                                    }
                                }
                            }
                        }
                    }
            }
        }
    }
}
