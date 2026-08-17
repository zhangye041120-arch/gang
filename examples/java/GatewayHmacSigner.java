package com.xiaoliao.gateway;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public final class GatewayHmacSigner {
    private GatewayHmacSigner() {}

    public static Map<String, String> signedHeaders(
            String serviceToken,
            String hmacSecret,
            String method,
            String path,
            String userId,
            byte[] body) throws Exception {
        long timestamp = Instant.now().getEpochSecond();
        String nonce = UUID.randomUUID().toString();
        String bodySha256 = hex(MessageDigest.getInstance("SHA-256").digest(body));
        String canonical = String.join("\n",
                Long.toString(timestamp),
                nonce,
                method.toUpperCase(),
                path,
                userId,
                bodySha256);

        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(
                hmacSecret.getBytes(StandardCharsets.UTF_8),
                "HmacSHA256"));
        String signature = hex(mac.doFinal(canonical.getBytes(StandardCharsets.US_ASCII)));

        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("Authorization", "Bearer " + serviceToken);
        headers.put("Content-Type", "application/json; charset=utf-8");
        headers.put("X-Gateway-Timestamp", Long.toString(timestamp));
        headers.put("X-Gateway-Nonce", nonce);
        headers.put("X-Gateway-User-ID", userId);
        headers.put("X-Gateway-Signature", signature);
        return headers;
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) {
            result.append(String.format("%02x", item & 0xff));
        }
        return result.toString();
    }
}
