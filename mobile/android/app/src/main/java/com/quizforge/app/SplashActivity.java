package com.quizforge.app;

import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 启动页：在后台把 Python 服务拉起来，健康检查通过后再交给 MainActivity。
 *
 * 为什么不是「直接让 WebView 加载 127.0.0.1」：Capacitor 的 WebView 一创建就会
 * 去 loadUrl，那时 uvicorn 还没开始监听 —— 用户看到的是一片白，而且没有任何
 * 线索说明是"正在启动"还是"起不来"。所以这里先用原生界面占住屏幕，
 * 把真实进度（释放资源 / 起解释器 / 等端口）写在屏幕上。
 *
 * 线程模型：所有重活都在 boot 线程做，UI 只通过 Handler 收状态。
 * 异常不往上抛 —— 显示在屏幕上，并指向 filesDir/android_boot.log。
 */
public class SplashActivity extends AppCompatActivity {
    private static final String TAG = "QFBOOT";
    private static final int PORT = 8731;
    /** Python 第一次启动要解包运行时，给足时间；之后的启动通常 2~4 秒。 */
    private static final int HEALTH_TIMEOUT_S = 120;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final AtomicBoolean moved = new AtomicBoolean(false);
    private TextView status;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_splash);
        status = findViewById(R.id.splash_status);
        new Thread(this::boot, "qf-boot").start();
    }

    private void say(String text) {
        Log.i(TAG, text);
        ui.post(() -> status.setText(text));
    }

    private void boot() {
        try {
            say("正在释放前端资源…");
            say(BootHelper.prepare(this));

            say("正在启动 Python…");
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(getApplicationContext()));
            }

            say("正在启动后端服务…");
            Python.getInstance()
                    .getModule("android_boot")
                    .callAttr("start_in_thread",
                            BootHelper.dataDir(this).getAbsolutePath(),
                            BootHelper.webDir(this).getAbsolutePath(),
                            PORT);

            say("等待服务就绪…");
            if (!waitHealthy()) {
                fail("后端在 " + HEALTH_TIMEOUT_S + " 秒内没有就绪");
                return;
            }

            say("就绪");
            if (moved.compareAndSet(false, true)) {
                ui.post(() -> {
                    startActivity(new Intent(this, MainActivity.class));
                    finish();
                });
            }
        } catch (Throwable t) {
            Log.e(TAG, "boot failed", t);
            fail(t.getClass().getSimpleName() + ": " + t.getMessage());
        }
    }

    /** 每秒问一次 /api/health。返回 true 表示 200。 */
    private boolean waitHealthy() {
        for (int i = 1; i <= HEALTH_TIMEOUT_S; i++) {
            try {
                HttpURLConnection conn = (HttpURLConnection)
                        new URL("http://127.0.0.1:" + PORT + "/api/health").openConnection();
                conn.setConnectTimeout(1200);
                conn.setReadTimeout(1200);
                if (conn.getResponseCode() == 200) {
                    return true;
                }
            } catch (IOException ignored) {
                // 还没监听 —— 正常，继续等
            }
            if (i % 5 == 0) {
                say("等待服务就绪… " + i + "s");
            }
            try {
                Thread.sleep(1000);
            } catch (InterruptedException exc) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
        return false;
    }

    /** 失败时把日志尾部贴出来 —— 没有控制台，这是唯一能让用户说清楚"错在哪"的东西。 */
    private void fail(String reason) {
        String tail = tail(readLog(), 20);
        String text = "启动失败：" + reason + (tail.isEmpty() ? "" : "\n\n" + tail);
        ui.post(() -> status.setText(text));
    }

    private String readLog() {
        File log = new File(BootHelper.dataDir(this), "android_boot.log");
        if (!log.isFile()) {
            return "";
        }
        try {
            return new String(java.nio.file.Files.readAllBytes(log.toPath()), java.nio.charset.StandardCharsets.UTF_8);
        } catch (IOException exc) {
            return "（日志读不出来：" + exc.getMessage() + "）";
        }
    }

    private static String tail(String text, int lines) {
        String[] all = text.split("\n");
        int from = Math.max(0, all.length - lines);
        StringBuilder out = new StringBuilder();
        for (int i = from; i < all.length; i++) {
            out.append(all[i]).append('\n');
        }
        return out.toString().trim();
    }
}
