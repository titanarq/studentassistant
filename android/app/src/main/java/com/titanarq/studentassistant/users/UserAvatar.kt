package com.titanarq.studentassistant.users

import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.protocol.User

/** The user's photo (loaded through [photos], with the bearer token) in a circle, or their initials until it is there or when there is none. */
@Composable
fun UserAvatar(
    user: User,
    photos: UserPhotos,
    modifier: Modifier = Modifier,
    size: Dp = 40.dp,
    contentDescription: String? = stringResource(R.string.users_photo_description, user.name),
) {
    val version by photos.version.collectAsState()
    val photo by produceState<ImageBitmap?>(initialValue = null, user.id, user.photoUrl, version) {
        value = photos.load(user)?.let { bytes ->
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
        }
    }
    val shape = CircleShape
    Box(
        modifier = modifier.size(size).clip(shape).background(MaterialTheme.colorScheme.primaryContainer),
        contentAlignment = Alignment.Center,
    ) {
        val image = photo
        if (image != null) {
            Image(
                bitmap = image,
                contentDescription = contentDescription,
                contentScale = ContentScale.Crop,
                modifier = Modifier.size(size),
            )
        } else {
            Text(
                initialsOf(user.name),
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
        }
    }
}
