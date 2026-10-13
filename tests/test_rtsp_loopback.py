from segunda_tela.wfd.loopback import run_loopback_test


def test_rtsp_loopback():
    assert run_loopback_test(port=17237)
