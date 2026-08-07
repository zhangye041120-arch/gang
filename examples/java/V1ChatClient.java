import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.UUID;

public class V1ChatClient {
    public static void main(String[] args) throws Exception {
        String baseUrl = args.length > 0 ? args[0] : "http://127.0.0.1:8081";
        String token = args.length > 1 ? args[1] : System.getenv("XIAOLIAO_API_TOKEN");
        if (token == null || token.isBlank()) {
            throw new IllegalArgumentException("缺少 API Token：传入第二个参数或设置 XIAOLIAO_API_TOKEN");
        }

        String payload = "{"
            + "\"user_id\":\"user_001\","
            + "\"session_id\":\"session_001\","
            + "\"message\":\"我最近有点累\","
            + "\"context\":{\"consent\":{\"personalization\":false},\"user_summary\":\"\"},"
            + "\"debug\":false"
            + "}";

        HttpClient client = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(10))
            .build();
        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(baseUrl + "/v1/chat"))
            .timeout(Duration.ofSeconds(60))
            .header("Content-Type", "application/json; charset=UTF-8")
            .header("Authorization", "Bearer " + token)
            .header("Idempotency-Key", UUID.randomUUID().toString())
            .header("X-Request-ID", UUID.randomUUID().toString())
            .POST(HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8))
            .build();

        HttpResponse<String> response = client.send(
            request,
            HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8)
        );
        System.out.println("HTTP " + response.statusCode());
        System.out.println("X-Request-ID: " + response.headers().firstValue("X-Request-ID").orElse(""));
        System.out.println(response.body());
    }
}
