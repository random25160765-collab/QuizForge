package com.quizforge.app;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.util.Base64;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.core.content.FileProvider;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.getcapacitor.BridgeActivity;

import java.io.File;
import java.io.FileOutputStream;

/**
 * 主界面：一个普通的 Capacitor BridgeActivity。
 *
 * 它加载什么由 `capacitor.config.json` 的 `server.url` 决定
 * （`http://127.0.0.1:8731`），而那个地址要等 Python 服务起来才有人应答 ——
 * 所以启动流程是 SplashActivity → 这里，见 SplashActivity 的注释。
 *
 * ## 状态栏 / 手势条留白（2026-11-09）
 *
 * targetSdk 36，Android 15 起强制 edge-to-edge，WebView 会一直画到系统栏底下
 * （实测：顶栏那排按钮被时间、信号图标压住；竖屏一样）。Android 的 WebView
 * 不像 iOS 那样给 `env(safe-area-inset-*)` 值（那里恒为 0），所以只能在原生侧
 * 把系统栏占的高度让出来 —— 给 **Activity 的 content 容器**加 padding。
 * 加在 WebView 自己身上会被 Capacitor 的布局重置，别那样写。
 *
 * 窗口背景取**浅色档的页面底色**（`--bg` = #F7F9FB），让出来的那一条才不会
 * 是一道别的颜色。深色档还没做，等做的时候这里要跟着主题走。
 *
 * ## 给页面的一小把原生能力（`window.QFNative`）
 *
 * * `readClipboard()` / `writeClipboard(text)` —— WebView 的 Clipboard API
 *   要安全上下文 + 权限回调，实测不可靠；直接读系统剪贴板最稳。读剪贴板
 *   在 Android 10+ 对**前台应用**不需要任何权限，所以 manifest 里不必加。
 * * `shareHtml(name, base64)` —— 分享页是一个自带数据的 HTML。WebView 里
 *   `a[download]` 不会落盘（没有 DownloadListener），所以把字节交给原生：
 *   写进 cache，再用 FileProvider + ACTION_SEND 走系统分享（可存到文件、
 *   发到微信/邮件）。不需要存储权限。
 */
public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().setBackgroundDrawable(new ColorDrawable(Color.rgb(0xF7, 0xF9, 0xFB)));

        View content = findViewById(android.R.id.content);
        if (content != null) {
            ViewCompat.setOnApplyWindowInsetsListener(content, (view, insets) -> {
                Insets bars = insets.getInsets(
                        WindowInsetsCompat.Type.systemBars()
                                | WindowInsetsCompat.Type.displayCutout());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
                return insets;
            });
            ViewCompat.requestApplyInsets(content);
        }

        WebView web = getBridge() != null ? getBridge().getWebView() : null;
        if (web != null) {
            web.addJavascriptInterface(new NativeBridge(), "QFNative");
        }
    }

    /** 页面侧的对象名就是 `QFNative`，方法在 `theme/runtime/shell.js` / `chat.js` 里调。 */
    private class NativeBridge {

        @JavascriptInterface
        public String readClipboard() {
            try {
                ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                if (cm == null || !cm.hasPrimaryClip()) return "";
                ClipData clip = cm.getPrimaryClip();
                if (clip == null || clip.getItemCount() == 0) return "";
                CharSequence text = clip.getItemAt(0).coerceToText(MainActivity.this);
                return text == null ? "" : text.toString();
            } catch (Exception err) {
                return "";
            }
        }

        @JavascriptInterface
        public void writeClipboard(String text) {
            try {
                ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                if (cm != null) cm.setPrimaryClip(ClipData.newPlainText("QuizForge", text == null ? "" : text));
            } catch (Exception err) {
                // 写不进去就算了，页面上还有手动复制
            }
        }

        @JavascriptInterface
        public void shareHtml(String name, String base64) {
            try {
                byte[] bytes = Base64.decode(base64, Base64.DEFAULT);
                File dir = new File(getCacheDir(), "share");
                if (!dir.isDirectory() && !dir.mkdirs()) {
                    throw new IllegalStateException("建不出分享目录");
                }
                String safe = (name == null || name.trim().isEmpty()) ? "chat.html" : name.trim();
                File file = new File(dir, safe);
                try (FileOutputStream out = new FileOutputStream(file)) {
                    out.write(bytes);
                }
                Uri uri = FileProvider.getUriForFile(
                        MainActivity.this, getPackageName() + ".fileprovider", file);
                Intent send = new Intent(Intent.ACTION_SEND);
                send.setType("text/html");
                send.putExtra(Intent.EXTRA_STREAM, uri);
                send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                startActivity(Intent.createChooser(send, "分享对话"));
            } catch (Exception err) {
                Toast.makeText(MainActivity.this, "分享失败：" + err.getMessage(), Toast.LENGTH_LONG).show();
            }
        }
    }
}
