"""斐波那契数列单元测试"""

import pytest
from fibonacci import fibonacci, fibonacci_sequence


class TestFibonacci:
    """测试fibonacci函数"""

    def test_base_case_zero(self):
        """测试基准情况: F(0) = 0"""
        assert fibonacci(0) == 0

    def test_base_case_one(self):
        """测试基准情况: F(1) = 1"""
        assert fibonacci(1) == 1

    def test_small_values(self):
        """测试小数值"""
        assert fibonacci(2) == 1
        assert fibonacci(3) == 2
        assert fibonacci(4) == 3
        assert fibonacci(5) == 5
        assert fibonacci(6) == 8
        assert fibonacci(7) == 13
        assert fibonacci(8) == 21
        assert fibonacci(9) == 34
        assert fibonacci(10) == 55

    def test_larger_values(self):
        """测试较大数值"""
        assert fibonacci(20) == 6765
        assert fibonacci(30) == 832040

    def test_negative_input(self):
        """测试负数输入应该抛出异常"""
        with pytest.raises(ValueError, match="n必须是非负整数"):
            fibonacci(-1)
        with pytest.raises(ValueError, match="n必须是非负整数"):
            fibonacci(-10)


class TestFibonacciSequence:
    """测试fibonacci_sequence函数"""

    def test_empty_sequence(self):
        """测试空序列"""
        assert fibonacci_sequence(0) == []

    def test_single_element(self):
        """测试单元素序列"""
        assert fibonacci_sequence(1) == [0]

    def test_two_elements(self):
        """测试两元素序列"""
        assert fibonacci_sequence(2) == [0, 1]

    def test_small_sequence(self):
        """测试小序列"""
        assert fibonacci_sequence(5) == [0, 1, 1, 2, 3]
        assert fibonacci_sequence(10) == [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]

    def test_negative_count(self):
        """测试负数count应该抛出异常"""
        with pytest.raises(ValueError, match="count必须是非负整数"):
            fibonacci_sequence(-1)

    def test_consistency_with_fibonacci(self):
        """测试sequence和单个fibonacci函数结果一致"""
        seq = fibonacci_sequence(15)
        for i in range(15):
            assert seq[i] == fibonacci(i), f"第{i}项不一致"


class TestFibonacciProperties:
    """测试斐波那契数列的数学性质"""

    def test_fibonacci_identity(self):
        """测试斐波那契恒等式: F(n) = F(n-1) + F(n-2)"""
        for n in range(2, 20):
            assert fibonacci(n) == fibonacci(n - 1) + fibonacci(n - 2)

    def test_fibonacci_is_monotonic(self):
        """测试斐波那契数列单调递增（从第2项开始）"""
        # F(0)=0, F(1)=1, F(2)=1, 所以从n=3开始严格递增
        for n in range(3, 20):
            assert fibonacci(n) > fibonacci(n - 1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
