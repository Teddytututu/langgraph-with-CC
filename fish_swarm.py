"""
Fish Swarm Simulation using Boids Algorithm
- Fish are represented as triangles
- Deep blue background (#001133)
- Mouse click adds food that attracts fish
"""

import pygame
import math
import random
from typing import List, Tuple

# Initialize Pygame
pygame.init()

# Screen settings
WIDTH = 1200
HEIGHT = 800
FPS = 60

# Colors
BACKGROUND_COLOR = (0, 17, 51)  # #001133
FISH_COLOR = (100, 200, 255)    # Light blue fish
FISH_OUTLINE = (150, 230, 255)  # Lighter outline
FOOD_COLOR = (255, 200, 100)    # Golden food
FOOD_GLOW = (255, 220, 150)     # Food glow

# Boids parameters
SEPARATION_DISTANCE = 25
ALIGNMENT_DISTANCE = 50
COHESION_DISTANCE = 100
SEPARATION_FORCE = 0.05
ALIGNMENT_FORCE = 0.03
COHESION_FORCE = 0.02
FOOD_ATTRACTION_FORCE = 0.08
MAX_SPEED = 4
MIN_SPEED = 1
MAX_FORCE = 0.15

# Fish settings
FISH_SIZE = 12
INITIAL_FISH_COUNT = 80


class Vector2D:
    """2D Vector class for physics calculations"""
    def __init__(self, x: float = 0, y: float = 0):
        self.x = x
        self.y = y

    def __add__(self, other):
        return Vector2D(self.x + other.x, self.y + other.y)

    def __sub__(self, other):
        return Vector2D(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar):
        return Vector2D(self.x * scalar, self.y * scalar)

    def __truediv__(self, scalar):
        if scalar == 0:
            return Vector2D(0, 0)
        return Vector2D(self.x / scalar, self.y / scalar)

    def magnitude(self) -> float:
        return math.sqrt(self.x ** 2 + self.y ** 2)

    def normalize(self):
        mag = self.magnitude()
        if mag > 0:
            return Vector2D(self.x / mag, self.y / mag)
        return Vector2D(0, 0)

    def limit(self, max_val: float):
        mag = self.magnitude()
        if mag > max_val:
            return self.normalize() * max_val
        return Vector2D(self.x, self.y)

    def distance_to(self, other) -> float:
        return (self - other).magnitude()

    def to_tuple(self) -> Tuple[float, float]:
        return (self.x, self.y)


class Fish:
    """Fish class implementing Boids behavior"""
    def __init__(self, x: float, y: float):
        self.position = Vector2D(x, y)
        angle = random.uniform(0, 2 * math.pi)
        speed = random.uniform(MIN_SPEED, MAX_SPEED)
        self.velocity = Vector2D(math.cos(angle) * speed, math.sin(angle) * speed)
        self.acceleration = Vector2D(0, 0)
        self.size = FISH_SIZE
        # Slight color variation
        self.color_variation = random.randint(-20, 20)

    def get_color(self) -> Tuple[int, int, int]:
        r = max(0, min(255, FISH_COLOR[0] + self.color_variation))
        g = max(0, min(255, FISH_COLOR[1] + self.color_variation))
        b = max(0, min(255, FISH_COLOR[2] + self.color_variation))
        return (r, g, b)

    def apply_force(self, force: Vector2D):
        self.acceleration = self.acceleration + force

    def separation(self, fishes: List['Fish']) -> Vector2D:
        """Steer away from nearby fish"""
        steering = Vector2D(0, 0)
        count = 0

        for other in fishes:
            if other is not self:
                distance = self.position.distance_to(other.position)
                if 0 < distance < SEPARATION_DISTANCE:
                    diff = self.position - other.position
                    diff = diff.normalize() / max(distance, 0.1)
                    steering = steering + diff
                    count += 1

        if count > 0:
            steering = steering / count
            if steering.magnitude() > 0:
                steering = steering.normalize() * MAX_SPEED - self.velocity
                steering = steering.limit(MAX_FORCE)

        return steering * SEPARATION_FORCE

    def alignment(self, fishes: List['Fish']) -> Vector2D:
        """Align velocity with nearby fish"""
        avg_velocity = Vector2D(0, 0)
        count = 0

        for other in fishes:
            if other is not self:
                distance = self.position.distance_to(other.position)
                if 0 < distance < ALIGNMENT_DISTANCE:
                    avg_velocity = avg_velocity + other.velocity
                    count += 1

        if count > 0:
            avg_velocity = avg_velocity / count
            avg_velocity = avg_velocity.normalize() * MAX_SPEED
            steering = avg_velocity - self.velocity
            steering = steering.limit(MAX_FORCE)
            return steering * ALIGNMENT_FORCE

        return Vector2D(0, 0)

    def cohesion(self, fishes: List['Fish']) -> Vector2D:
        """Steer towards center of nearby fish"""
        center = Vector2D(0, 0)
        count = 0

        for other in fishes:
            if other is not self:
                distance = self.position.distance_to(other.position)
                if 0 < distance < COHESION_DISTANCE:
                    center = center + other.position
                    count += 1

        if count > 0:
            center = center / count
            return self.seek(center) * COHESION_FORCE

        return Vector2D(0, 0)

    def seek_food(self, foods: List['Food']) -> Vector2D:
        """Seek nearest food"""
        if not foods:
            return Vector2D(0, 0)

        nearest_food = None
        min_distance = float('inf')

        for food in foods:
            distance = self.position.distance_to(food.position)
            if distance < min_distance:
                min_distance = distance
                nearest_food = food

        if nearest_food and min_distance < 200:
            return self.seek(nearest_food.position) * FOOD_ATTRACTION_FORCE

        return Vector2D(0, 0)

    def seek(self, target: Vector2D) -> Vector2D:
        """Calculate steering force towards target"""
        desired = target - self.position
        desired = desired.normalize() * MAX_SPEED
        steering = desired - self.velocity
        steering = steering.limit(MAX_FORCE)
        return steering

    def edges(self):
        """Wrap around screen edges"""
        if self.position.x > WIDTH:
            self.position.x = 0
        elif self.position.x < 0:
            self.position.x = WIDTH

        if self.position.y > HEIGHT:
            self.position.y = 0
        elif self.position.y < 0:
            self.position.y = HEIGHT

    def flock(self, fishes: List['Fish'], foods: List['Food']):
        """Apply all boids rules"""
        sep = self.separation(fishes)
        ali = self.alignment(fishes)
        coh = self.cohesion(fishes)
        food_force = self.seek_food(foods)

        self.apply_force(sep)
        self.apply_force(ali)
        self.apply_force(coh)
        self.apply_force(food_force)

    def update(self):
        """Update fish position"""
        self.velocity = self.velocity + self.acceleration

        # Limit speed
        speed = self.velocity.magnitude()
        if speed > MAX_SPEED:
            self.velocity = self.velocity.normalize() * MAX_SPEED
        elif speed < MIN_SPEED:
            self.velocity = self.velocity.normalize() * MIN_SPEED

        self.position = self.position + self.velocity
        self.acceleration = Vector2D(0, 0)
        self.edges()

    def draw(self, screen):
        """Draw fish as a triangle pointing in direction of velocity"""
        angle = math.atan2(self.velocity.y, self.velocity.x)

        # Triangle points
        front = (
            self.position.x + math.cos(angle) * self.size,
            self.position.y + math.sin(angle) * self.size
        )
        back_left = (
            self.position.x + math.cos(angle + 2.5) * self.size * 0.7,
            self.position.y + math.sin(angle + 2.5) * self.size * 0.7
        )
        back_right = (
            self.position.x + math.cos(angle - 2.5) * self.size * 0.7,
            self.position.y + math.sin(angle - 2.5) * self.size * 0.7
        )

        # Draw fish body
        pygame.draw.polygon(screen, self.get_color(), [front, back_left, back_right])
        pygame.draw.polygon(screen, FISH_OUTLINE, [front, back_left, back_right], 1)


class Food:
    """Food class that fish are attracted to"""
    def __init__(self, x: float, y: float):
        self.position = Vector2D(x, y)
        self.size = 6
        self.lifetime = 600  # Frames until food disappears
        self.pulse = 0

    def update(self) -> bool:
        """Update food state, return False if food should be removed"""
        self.lifetime -= 1
        self.pulse = (self.pulse + 0.1) % (2 * math.pi)
        return self.lifetime > 0

    def draw(self, screen):
        """Draw food with pulsing glow effect"""
        pulse_size = self.size + math.sin(self.pulse) * 2

        # Draw glow
        glow_size = pulse_size * 2
        glow_surface = pygame.Surface((int(glow_size * 4), int(glow_size * 4)), pygame.SRCALPHA)
        alpha = int(100 + math.sin(self.pulse) * 30)
        pygame.draw.circle(glow_surface, (*FOOD_GLOW, alpha),
                          (int(glow_size * 2), int(glow_size * 2)), int(glow_size))
        screen.blit(glow_surface,
                   (int(self.position.x - glow_size * 2), int(self.position.y - glow_size * 2)))

        # Draw food
        pygame.draw.circle(screen, FOOD_COLOR,
                          (int(self.position.x), int(self.position.y)), int(pulse_size))


class FishSwarmSimulation:
    """Main simulation class"""
    def __init__(self):
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT))
        pygame.display.set_caption("Fish Swarm Simulation - Boids Algorithm")
        self.clock = pygame.time.Clock()
        self.fishes: List[Fish] = []
        self.foods: List[Food] = []
        self.running = True
        self.paused = False

        # Initialize fish
        self.spawn_fish(INITIAL_FISH_COUNT)

        # Font for UI
        self.font = pygame.font.Font(None, 24)

    def spawn_fish(self, count: int):
        """Spawn fish at random positions"""
        for _ in range(count):
            x = random.uniform(50, WIDTH - 50)
            y = random.uniform(50, HEIGHT - 50)
            self.fishes.append(Fish(x, y))

    def handle_events(self):
        """Handle user input"""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:  # Left click - add food
                    self.foods.append(Food(event.pos[0], event.pos[1]))
                elif event.button == 3:  # Right click - add fish
                    self.fishes.append(Fish(event.pos[0], event.pos[1]))
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_SPACE:
                    self.paused = not self.paused
                elif event.key == pygame.K_r:
                    # Reset simulation
                    self.fishes.clear()
                    self.foods.clear()
                    self.spawn_fish(INITIAL_FISH_COUNT)
                elif event.key == pygame.K_f:
                    # Add 10 fish
                    self.spawn_fish(10)

    def update(self):
        """Update simulation state"""
        if self.paused:
            return

        # Update fish
        for fish in self.fishes:
            fish.flock(self.fishes, self.foods)
            fish.update()

        # Update food and check for fish eating it
        foods_to_remove = []
        for food in self.foods:
            if not food.update():
                foods_to_remove.append(food)
            else:
                # Check if any fish is close enough to eat the food
                for fish in self.fishes:
                    if fish.position.distance_to(food.position) < 15:
                        foods_to_remove.append(food)
                        break

        for food in foods_to_remove:
            if food in self.foods:
                self.foods.remove(food)

    def draw(self):
        """Draw everything"""
        self.screen.fill(BACKGROUND_COLOR)

        # Draw subtle water gradient effect
        for i in range(0, HEIGHT, 50):
            alpha = int(5 + (i / HEIGHT) * 10)
            pygame.draw.line(self.screen, (0, 30, 60 + alpha // 2), (0, i), (WIDTH, i))

        # Draw foods
        for food in self.foods:
            food.draw(self.screen)

        # Draw fish
        for fish in self.fishes:
            fish.draw(self.screen)

        # Draw UI
        self.draw_ui()

        pygame.display.flip()

    def draw_ui(self):
        """Draw user interface"""
        # Instructions
        instructions = [
            "Left Click: Add Food",
            "Right Click: Add Fish",
            "Space: Pause/Resume",
            "R: Reset",
            "F: Add 10 Fish"
        ]

        y_offset = 10
        for text in instructions:
            surface = self.font.render(text, True, (150, 180, 200))
            self.screen.blit(surface, (10, y_offset))
            y_offset += 20

        # Stats
        stats = [
            f"Fish: {len(self.fishes)}",
            f"Food: {len(self.foods)}",
            f"FPS: {int(self.clock.get_fps())}"
        ]

        y_offset = HEIGHT - 70
        for text in stats:
            surface = self.font.render(text, True, (150, 180, 200))
            self.screen.blit(surface, (10, y_offset))
            y_offset += 20

        # Paused indicator
        if self.paused:
            pause_text = self.font.render("PAUSED", True, (255, 200, 100))
            text_rect = pause_text.get_rect(center=(WIDTH // 2, 30))
            self.screen.blit(pause_text, text_rect)

    def run(self):
        """Main game loop"""
        while self.running:
            self.handle_events()
            self.update()
            self.draw()
            self.clock.tick(FPS)

        pygame.quit()


def main():
    simulation = FishSwarmSimulation()
    simulation.run()


if __name__ == "__main__":
    main()
