import claude


# 打印当前 claude 包的版本号
def cmd_version() -> None:
    print(claude.__version__)