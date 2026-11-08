package com.quizforge.app;

import com.getcapacitor.BridgeActivity;

/**
 * 主界面：一个普通的 Capacitor BridgeActivity。
 *
 * 它加载什么由 `capacitor.config.json` 的 `server.url` 决定
 * （`http://127.0.0.1:8731`），而那个地址要等 Python 服务起来才有人应答 ——
 * 所以启动流程是 SplashActivity → 这里，见 SplashActivity 的注释。
 */
public class MainActivity extends BridgeActivity {}
