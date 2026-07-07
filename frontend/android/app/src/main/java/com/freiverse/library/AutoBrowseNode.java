package com.freiverse.library;

import android.graphics.Bitmap;
import androidx.annotation.Nullable;

/** One row in the Android Auto media browse tree (from JS). */
public final class AutoBrowseNode {

    public final String mediaId;
    public final String title;
    public final String subtitle;
    public final boolean browsable;
    @Nullable public final String iconUri;
    @Nullable public final Bitmap iconBitmap;

    public AutoBrowseNode(
        String mediaId,
        String title,
        String subtitle,
        boolean browsable,
        @Nullable String iconUri,
        @Nullable Bitmap iconBitmap
    ) {
        this.mediaId = mediaId != null ? mediaId : "";
        this.title = title != null ? title : "";
        this.subtitle = subtitle != null ? subtitle : "";
        this.browsable = browsable;
        this.iconUri = iconUri;
        this.iconBitmap = iconBitmap;
    }
}
