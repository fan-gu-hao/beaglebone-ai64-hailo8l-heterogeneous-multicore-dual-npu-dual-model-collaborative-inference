#include <hailo/hailort.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <vector>

using namespace hailort;
#define CHECK(x) do { if (!(x)) throw std::runtime_error( \
    std::string(#x) + "，錯誤碼=" + std::to_string((x).status())); } while (0)

int main(int argc, char **argv)
{
    try {
        if (argc != 2) throw std::runtime_error("請指定 HEF 模型路徑");
        std::ios::sync_with_stdio(false);
        std::cin.tie(nullptr);
        auto dev = VDevice::create(); CHECK(dev);
        auto hef = Hef::create(argv[1]); CHECK(hef);
        auto config = dev.value()->create_configure_params(hef.value()); CHECK(config);
        for (auto &item : config.value()) item.second.batch_size = 1;
        auto groups = dev.value()->configure(hef.value(), config.value()); CHECK(groups);
        if (groups->size() != 1) throw std::runtime_error("模型群組數量不符");
        auto group = groups->at(0);
        auto ip = group->make_input_vstream_params(false, HAILO_FORMAT_TYPE_UINT8, 5000, 2);
        auto op = group->make_output_vstream_params(false, HAILO_FORMAT_TYPE_FLOAT32, 5000, 2);
        CHECK(ip); CHECK(op);
        for (auto &item : ip.value()) item.second.user_buffer_format.order = HAILO_FORMAT_ORDER_NHWC;
        for (auto &item : op.value()) item.second.user_buffer_format.order = HAILO_FORMAT_ORDER_HAILO_NMS_BY_CLASS;
        auto pipeline = InferVStreams::create(*group, ip.value(), op.value()); CHECK(pipeline);
        auto inputs = pipeline->get_input_vstreams();
        auto outputs = pipeline->get_output_vstreams();
        if (inputs.size() != 1 || outputs.size() != 1)
            throw std::runtime_error("需要單一影像輸入與單一 NMS 輸出");
        auto &input = inputs[0].get();
        auto &output = outputs[0].get();
        const auto shape = input.get_info().shape;
        const auto nms = output.get_info().nms_shape;
        if (shape.height != 640 || shape.width != 640 || shape.features != 3 ||
            input.get_frame_size() != 640 * 640 * 3 || nms.number_of_classes != 80 ||
            output.get_frame_size() % sizeof(float) != 0)
            throw std::runtime_error("模型形狀與預期 YOLOv8n 不符");
        std::vector<uint8_t> rgb(input.get_frame_size());
        std::vector<float> boxes(output.get_frame_size() / sizeof(float));
        std::map<std::string, MemoryView> in_views{
            {input.name(), MemoryView(rgb.data(), rgb.size())}};
        std::map<std::string, MemoryView> out_views{
            {output.name(), MemoryView(boxes.data(), boxes.size() * sizeof(float))}};
        std::cout << "{\"ready\":true,\"width\":640,\"height\":640}" << std::endl;
        uint64_t frame = 0;
        while (true) {
            std::cin.read(reinterpret_cast<char *>(rgb.data()), rgb.size());
            if (std::cin.gcount() == 0 && std::cin.eof()) break;
            if (std::cin.gcount() != static_cast<std::streamsize>(rgb.size()))
                throw std::runtime_error("輸入影像不完整");
            std::fill(boxes.begin(), boxes.end(), 0.0f);
            const auto start = std::chrono::steady_clock::now();
            const auto status = pipeline->infer(in_views, out_views, 1);
            if (status != HAILO_SUCCESS)
                throw std::runtime_error("推論失敗，錯誤碼=" + std::to_string(status));
            const double ms = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - start).count();
            std::ostringstream json;
            json << std::fixed << std::setprecision(6)
                 << "{\"frame\":" << ++frame << ",\"infer_ms\":" << ms << ",\"detections\":[";
            size_t pos = 0;
            bool first = true;
            for (uint32_t cls = 0; cls < nms.number_of_classes; ++cls) {
                if (pos >= boxes.size()) throw std::runtime_error("NMS 計數超出緩衝區");
                const float count = boxes[pos++];
                if (!std::isfinite(count) || count < 0 || count > nms.max_bboxes_per_class ||
                    std::floor(count) != count)
                    throw std::runtime_error("NMS 框數無效");
                for (uint32_t i = 0; i < static_cast<uint32_t>(count); ++i) {
                    if (pos + 5 > boxes.size()) throw std::runtime_error("NMS 座標超出緩衝區");
                    const float *b = &boxes[pos];
                    pos += 5;
                    bool finite = true;
                    for (int k = 0; k < 5; ++k) finite = finite && std::isfinite(b[k]);
                    if (!finite || b[4] < 0.35f || b[4] > 1.0f) continue;
                    if (cls != 0 && cls != 39 && cls != 41 && cls != 67) continue;
                    if (!first) json << ',';
                    first = false;
                    json << "{\"class_id\":" << cls << ",\"score\":" << b[4]
                         << ",\"box\":[" << b[0] << ',' << b[1] << ',' << b[2] << ',' << b[3] << "]}";
                }
            }
            json << "]}";
            std::cout << json.str() << std::endl;
        }
        return 0;
    } catch (const std::exception &e) {
        std::cerr << "Hailo 工作程式停止：" << e.what() << std::endl;
        return 1;
    }
}
