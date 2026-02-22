"""斐波那契数列计算模块"""


def fibonacci(n: int) -> int:
    """
    计算斐波那契数列的第n项

    使用动态规划方法，时间复杂度O(n)，空间复杂度O(1)

    Args:
        n: 斐波那契数列的索引（从0开始）
           F(0)=0, F(1)=1, F(2)=1, F(3)=2, ...

    Returns:
        斐波那契数列的第n项

    Raises:
        ValueError: 当n为负数时
    """
    if n < 0:
        raise ValueError("n必须是非负整数")

    if n == 0:
        return 0
    if n == 1:
        return 1

    prev, curr = 0, 1
    for _ in range(2, n + 1):
        prev, curr = curr, prev + curr

    return curr


def fibonacci_sequence(count: int) -> list[int]:
    """
    生成前n项斐波那契数列

    Args:
        count: 要生成的项数

    Returns:
        包含前n项斐波那契数的列表

    Raises:
        ValueError: 当count为负数时
    """
    if count < 0:
        raise ValueError("count必须是非负整数")

    if count == 0:
        return []

    if count == 1:
        return [0]

    result = [0, 1]
    for _ in range(2, count):
        result.append(result[-1] + result[-2])

    return result


if __name__ == "__main__":
    # 简单演示
    print("斐波那契数列前15项:")
    print(fibonacci_sequence(15))

    print("\n第20项:", fibonacci(20))
