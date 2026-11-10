package com.quizforge.app;

import android.content.Context;
import android.content.pm.PackageManager;
import android.content.res.AssetManager;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/**
 * 首启准备：把 APK 里的只读资源搬到应用私有目录。
 *
 * 为什么必须先搬：Python 的静态文件挂载与 SQLite 都要求真实文件路径，
 * 而 assets 在 APK 里是压缩包内的条目，不能直接当目录用。
 *
 * 什么时候重搬：用**包的 lastUpdateTime** 当版本戳 —— 覆盖安装后它一定会变，
 * 于是升级 APK 时 web/ 会自动刷新；日常启动则一次都不拷（25M、上千个文件）。
 */
final class BootHelper {
    private static final String WEB_STAMP = ".asset-stamp";

    private BootHelper() {}

    static File dataDir(Context context) {
        return new File(context.getFilesDir(), "quizforge");
    }

    static File webDir(Context context) {
        return new File(context.getFilesDir(), "web");
    }

    /** 搬 web/，必要时把内置题库拷成工作库。返回一句可读的进度说明。 */
    static String prepare(Context context) throws IOException, PackageManager.NameNotFoundException {
        File web = webDir(context);
        String stamp = String.valueOf(
                context.getPackageManager().getPackageInfo(context.getPackageName(), 0).lastUpdateTime);
        File stampFile = new File(web, WEB_STAMP);
        boolean fresh = !stampFile.isFile()
                || !stamp.equals(new String(Files.readAllBytes(stampFile.toPath()), StandardCharsets.UTF_8));
        if (fresh) {
            deleteRecursively(web);
            copyAssetDir(context.getAssets(), "web", web);
            try (OutputStream out = new FileOutputStream(stampFile)) {
                out.write(stamp.getBytes(StandardCharsets.UTF_8));
            }
        }

        File db = new File(dataDir(context), "quizforge.db");
        if (!db.isFile()) {
            // java.io.File#getParentFile 可能为 null（dataDir 就是 filesDir 时不会，但别赌）
            File parent = db.getParentFile();
            if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
                throw new IOException("建不出数据目录：" + parent);
            }
            // 安装包里不放数据：题库由首启同步从 Turso 整包拉（app/bank_cloud.py）。
            // 这里只留一个空库，Python 侧 `ensure_schema` 会把表建齐。
            db.createNewFile();
        }

        // 可选的同步引导（构建时用 scripts/inject-sync-assets.py 塞进来）：
        // 一份设置文件里同时给出 turso 地址/令牌与对话端到端口令（`syncPassphrase`）。
        // 只在目标不存在时拷 —— 用户在设置页改过之后就以设备上那份为准。
        copyAssetIfAbsent(context.getAssets(), "sync/settings.local.json",
                new File(dataDir(context), "config/settings.local.json"));

        // Pyodide 的校验清单（3KB，随包）：手机没有 vendor/ 副本，只能按清单下载，
        // 而清单是校验依据 —— 缺了它 heavy_deps 会拒绝下载（"不敢下"）。
        // **每次都覆盖**：清单随版本更新，不能像设置文件那样"已有就不动"；
        // 老包没有这个 asset 时静默跳过，只影响"跑 Python"这一项。
        copyAssetIfPresent(context.getAssets(), "runtime/pyodide-manifest.json",
                new File(dataDir(context), "config/pyodide-manifest.json"));

        // 清单里有、CDN 上拿不到的文件（自带的 schemdraw wheel）：149K，跟着包走。
        // 少了它，手机会在下到 22/27 时失败，前面那 171M 全白下。
        copyAssetDirIfPresent(context.getAssets(), "runtime/pyodide-bundle",
                new File(dataDir(context), "config/pyodide-bundle"));

        return fresh ? "已释放前端资源" : "资源已是最新";
    }

    /** assets 里没有这个文件是正常情况（没注入同步配置），不是错误。 */
    private static void copyAssetIfAbsent(AssetManager assets, String from, File to) throws IOException {
        if (to.exists()) {
            return;
        }
        copyAssetIfPresent(assets, from, to);
    }

    /** 有就拷（**覆盖目标**），没有就跳过 —— 只读资源随版本走，不能沿用旧的那份。 */
    private static void copyAssetIfPresent(AssetManager assets, String from, File to) throws IOException {
        try {
            assets.open(from).close();
        } catch (IOException missing) {
            return;
        }
        copyAsset(assets, from, to);
    }

    /** assets 下的一个目录：有就整个拷（覆盖目标），没有就跳过。 */
    private static void copyAssetDirIfPresent(AssetManager assets, String from, File to) throws IOException {
        String[] names = assets.list(from);
        if (names == null || names.length == 0) {
            return;
        }
        for (String name : names) {
            copyAsset(assets, from + "/" + name, new File(to, name));
        }
    }

    private static void copyAssetDir(AssetManager assets, String from, File to) throws IOException {
        String[] children = assets.list(from);
        if (children == null || children.length == 0) {
            copyAsset(assets, from, to);
            return;
        }
        if (!to.isDirectory() && !to.mkdirs()) {
            throw new IOException("建不出目录：" + to);
        }
        for (String child : children) {
            copyAssetDir(assets, from + "/" + child, new File(to, child));
        }
    }

    private static void copyAsset(AssetManager assets, String from, File to) throws IOException {
        File parent = to.getParentFile();
        if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
            throw new IOException("建不出目录：" + parent);
        }
        try (InputStream in = assets.open(from); OutputStream out = new FileOutputStream(to)) {
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = in.read(buffer)) > 0) {
                out.write(buffer, 0, read);
            }
        }
    }

    private static void deleteRecursively(File target) {
        if (target == null || !target.exists()) {
            return;
        }
        File[] children = target.listFiles();
        if (children != null) {
            for (File child : children) {
                deleteRecursively(child);
            }
        }
        // 删不掉不算错：下次启动会再试，最坏是多占一份空间
        //noinspection ResultOfMethodCallIgnored
        target.delete();
    }
}
