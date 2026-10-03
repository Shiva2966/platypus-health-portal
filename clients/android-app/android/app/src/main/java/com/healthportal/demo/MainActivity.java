package com.healthportal.demo;

import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;

import com.getcapacitor.BridgeActivity;

/**
 * Loads the local launcher page (www/index.html), which then navigates to the configured server URL.
 * Adds: download handling (PDFs etc. via DownloadManager, then open), and Back -> settings at the root.
 */
public class MainActivity extends BridgeActivity {

    private long lastDownloadId = -1;
    private String lastMime = null;

    private final BroadcastReceiver onDownloadDone = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            long id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1);
            if (id != lastDownloadId || id == -1) return;
            DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            Uri uri = dm.getUriForDownloadedFile(id);
            if (uri == null) {
                Toast.makeText(MainActivity.this, "Download failed (login expired or file unavailable).", Toast.LENGTH_LONG).show();
                return;
            }
            String mime = dm.getMimeTypeForDownloadedFile(id);
            if (mime == null || mime.isEmpty()) mime = lastMime;
            Intent view = new Intent(Intent.ACTION_VIEW);
            view.setDataAndType(uri, mime == null ? "*/*" : mime);
            view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                startActivity(view);
            } catch (Exception e) {
                Toast.makeText(MainActivity.this, "Saved to Downloads (no app found to open it).", Toast.LENGTH_LONG).show();
            }
        }
    };

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        final WebView web = getBridge().getWebView();
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        // Download / open files (PDFs, images) served by the portal. Session cookie is forwarded.
        web.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            if (url.startsWith("blob:") || url.startsWith("data:")) {
                Toast.makeText(this, "This download type is not supported in the app. Use the portal's Download button.", Toast.LENGTH_LONG).show();
                return;
            }
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                String cookies = CookieManager.getInstance().getCookie(url);
                if (cookies != null) req.addRequestHeader("Cookie", cookies);
                req.addRequestHeader("User-Agent", userAgent);
                String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
                req.setMimeType(mimeType);
                req.setTitle(name);
                req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                lastMime = mimeType;
                lastDownloadId = dm.enqueue(req);
                Toast.makeText(this, "Downloading " + name + " ...", Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(this, "Download error: " + e.getMessage(), Toast.LENGTH_LONG).show();
            }
        });

        IntentFilter f = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(onDownloadDone, f, Context.RECEIVER_EXPORTED);
        } else {
            registerReceiver(onDownloadDone, f);
        }

        // Back: navigate history; at the first page of the portal, open the settings screen.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                WebView w = getBridge().getWebView();
                String current = w.getUrl();
                boolean onSettings = current != null && current.startsWith(getBridge().getLocalUrl());
                if (onSettings) {
                    // Already on the settings/launcher page: leave the app.
                    moveTaskToBack(true);
                } else if (w.canGoBack()) {
                    w.goBack();
                } else {
                    moveTaskToBack(true); // no settings screen: Back at the first page just leaves the app
                }
            }
        });
    }

    @Override
    public void onDestroy() {
        try { unregisterReceiver(onDownloadDone); } catch (Exception ignored) {}
        super.onDestroy();
    }
}
