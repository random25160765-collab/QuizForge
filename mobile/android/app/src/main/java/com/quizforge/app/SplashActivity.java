package com.quizforge.app;

import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.ProgressBar;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 启动页：在后台把 Python 服务拉起来，健康检查通过后再交给 MainActivity。
 *
 * 为什么不是「直接让 WebView 加载 127.0.0.1」：Capacitor 的 WebView 一创建就会
 * 去 loadUrl，那时 uvicorn 还没开始监听 —— 用户看到的是一片白，而且没有任何
 * 线索说明是"正在启动"还是"起不来"。所以这里先用原生界面占住屏幕，
 * 把真实进度（释放资源 / 起解释器 / 等端口）写在屏幕上。
 *
 * 2026-11-10 增加一段：**必要的重型依赖（Pyodide 146M）在这一屏下完**，带百分比。
 * 用户原话："大的依赖还是不该让它在背后自己下，在首次安装的时候显示进度条，
 * 表示正在下载必要的依赖中。" 所以这里等 `/api/heavy/status` 走完才进主界面；
 * 下不动（没网 / 被墙 / 超时）就显示原因 + 一个"跳过"按钮，绝不把人卡死。
 *
 * 线程模型：所有重活都在 boot 线程做，UI 只通过 Handler 收状态。
 * 异常不往上抛 —— 显示在屏幕上，并指向 filesDir/android_boot.log。
 */
public class SplashActivity extends AppCompatActivity {
    private static final String TAG = "QFBOOT";
    private static final int PORT = 8731;
    /** Python 第一次启动要解包运行时，给足时间；之后的启动通常 2~4 秒。 */
    private static final int HEALTH_TIMEOUT_S = 120;
    /** 146M 走手机网络可能要好几分钟；总上限给 30 分钟。 */
    private static final int HEAVY_TIMEOUT_S = 30 * 60;
    /** 连续这么久一个字节都没涨，就当作下不动了。 */
    private static final int HEAVY_STALL_S = 90;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final AtomicBoolean moved = new AtomicBoolean(false);
    private TextView status;
    private ProgressBar progress;
    private TextView hint;
    private Button skip;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_splash);
        status = findViewById(R.id.splash_status);
        progress = findViewById(R.id.splash_progress);
        hint = findViewById(R.id.splash_hint);
        skip = findViewById(R.id.splash_skip);
        skip.setOnClickListener(view -> moveOn());
        new Thread(this::boot, "qf-boot").start();
    }

    private void say(String text) {
        Log.i(TAG, text);
        ui.post(() -> status.setText(text));
    }

    private void boot() {
        try {
            // 屏上要说**在做什么**，不是只给一个读秒（用户 2026-11-10）。
            // 每一步一行短语，秒数只作为"还没卡死"的附注。
            say("正在解锁前端资源");
            Log.i(TAG, "释放前端资源：" + BootHelper.prepare(this));

            say("正在启动 Python 解释器");
            Log.i(TAG, "启动 Python");
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(getApplicationContext()));
            }

            say("正在启动后端服务");
            Log.i(TAG, "启动后端服务");
            Python.getInstance()
                    .getModule("android_boot")
                    .callAttr("start_in_thread",
                            BootHelper.dataDir(this).getAbsolutePath(),
                            BootHelper.webDir(this).getAbsolutePath(),
                            PORT);

            if (!waitHealthy()) {
                fail("后端在 " + HEALTH_TIMEOUT_S + " 秒内没有就绪");
                return;
            }

            say("正在检查运行环境");
            if (!waitHeavy()) {
                return;   // 已经显示原因 + 「跳过」按钮，等用户决定
            }

            moveOn();
        } catch (Throwable t) {
            Log.e(TAG, "boot failed", t);
            fail(t.getClass().getSimpleName() + ": " + t.getMessage());
        }
    }

    private void moveOn() {
        if (!moved.compareAndSet(false, true)) {
            return;
        }
        ui.post(() -> {
            startActivity(new Intent(this, MainActivity.class));
            finish();
        });
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
            // 第一秒就把"在做什么"写出来，之后每 3 秒补一次秒数（只作附注）
            if (i == 1 || i % 3 == 0) {
                say("正在启动后端服务 · " + i + "s");
            }
            if (!sleepOneSecond()) {
                return false;
            }
        }
        return false;
    }

    /**
     * 等必要依赖下完（首次安装才会真的下；之后缓存命中，几乎立刻返回）。
     *
     * 状态由后端 `heavy_deps` 提供：idle（还没开始）/ running / ok / failed。
     * 只有 ok 才继续；failed 或"长时间没有新字节"就停在启动页，给「跳过」。
     */
    private boolean waitHeavy() {
        long startedAt = System.currentTimeMillis();
        long lastDone = -1;
        long lastChangeAt = startedAt;
        while (System.currentTimeMillis() - startedAt < HEAVY_TIMEOUT_S * 1000L) {
            JSONObject state = readHeavyStatus();
            if (state != null) {
                String phase = state.optString("state", "");
                long done = state.optLong("done", 0);
                if ("ok".equals(phase)) {
                    showProgress(100, done, true);
                    return true;
                }
                if ("failed".equals(phase)) {
                    showSkip(state.optString("detail", "依赖没下成"));
                    return false;
                }
                if ("running".equals(phase)) {
                    if (done != lastDone) {
                        lastDone = done;
                        lastChangeAt = System.currentTimeMillis();
                    }
                    showProgress(state.optInt("percent", 0), done, false);
                }
            }
            if (System.currentTimeMillis() - lastChangeAt > HEAVY_STALL_S * 1000L) {
                showSkip("下载停住了（" + HEAVY_STALL_S + " 秒没有进展）");
                return false;
            }
            if (!sleepOneSecond()) {
                return false;
            }
        }
        showSkip("依赖下载超时");
        return false;
    }

    private void showProgress(int percent, long done, boolean doneOk) {
        ui.post(() -> {
            progress.setVisibility(View.VISIBLE);
            progress.setIndeterminate(percent <= 0 && !doneOk);
            progress.setProgress(Math.max(0, Math.min(100, percent)));
            if (doneOk) {
                status.setText("就绪");
                hint.setVisibility(View.GONE);
            } else {
                status.setText("正在下载必要的依赖 · " + percent + "%");
                hint.setVisibility(View.VISIBLE);
                hint.setText(String.format("已下载 %.0f MB", done / 1048576.0));
            }
        });
    }

    private void showSkip(String reason) {
        Log.w(TAG, "重型依赖没就绪：" + reason);
        ui.post(() -> {
            progress.setVisibility(View.GONE);
            hint.setVisibility(View.GONE);
            status.setText("必要的依赖没下成：" + reason + "\n先进入也可以，跑 Python 时会再试。");
            skip.setVisibility(View.VISIBLE);
        });
    }

    private JSONObject readHeavyStatus() {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection)
                    new URL("http://127.0.0.1:" + PORT + "/api/heavy/status").openConnection();
            conn.setConnectTimeout(1200);
            conn.setReadTimeout(2500);
            if (conn.getResponseCode() != 200) {
                return null;
            }
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8))) {
                StringBuilder body = new StringBuilder();
                String line;
                while ((line = reader.readLine()) != null) {
                    body.append(line);
                }
                return new JSONObject(body.toString());
            }
        } catch (Exception ignored) {
            return null;   // 后端刚起来时接口可能还没挂上 —— 下一轮再问
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private static boolean sleepOneSecond() {
        try {
            Thread.sleep(1000);
            return true;
        } catch (InterruptedException exc) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    /** 失败时把日志尾部贴出来 —— 没有控制台，这是唯一能让用户说清楚"错在哪"的东西。 */
    private void fail(String reason) {
        String tail = tail(readLog(), 20);
        String text = "启动失败：" + reason + (tail.isEmpty() ? "" : "\n\n" + tail);
        ui.post(() -> {
            progress.setVisibility(View.GONE);
            hint.setVisibility(View.GONE);
            status.setText(text);
        });
    }

    private String readLog() {
        File log = new File(BootHelper.dataDir(this), "android_boot.log");
        if (!log.isFile()) {
            return "";
        }
        try {
            return new String(java.nio.file.Files.readAllBytes(log.toPath()), StandardCharsets.UTF_8);
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
